"""Greenhouse security codes from the owner's Gmail, read over IMAP with an app password.

Read only: messages are fetched with BODY.PEEK (they stay unread) and nothing is moved,
deleted, sent or clicked. Only Greenhouse's sender is searched, only mail that arrived after
this job's Submit click counts, and the subject must name the job's company
("Security code for your application to Affirm"). Submits wait for their code one at a time,
so two back-to-back applications to one company can't swap codes; a code already used is
never used again. Anything unclear (no code, two codes) returns None and the job is staged.
"""

import contextlib
import email
import email.policy
import imaplib
import re
import subprocess
import time
from collections.abc import Callable, Iterable, Iterator
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser

from .answers.bank import normalize

IMAP_HOST = "imap.gmail.com"
MAILBOX = '"[Gmail]/All Mail"'  # every message, even one archived or filtered out of the inbox
KEYCHAIN_SERVICE = "auto-job-applier-gmail"
SENDER = "greenhouse-mail.io"
CODE_TIMEOUT_S = 4 * 60  # Greenhouse codes can take a couple of minutes to arrive
POLL_S = 10
CLOCK_SKEW = timedelta(seconds=30)  # mail server and laptop clocks differ a little
_CODE_LINE = re.compile(r"^[A-Za-z0-9]{8}$")


class _Lines(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = []

    def handle_data(self, data: str) -> None:
        self.lines += [line.strip() for line in data.splitlines() if line.strip()]


def _text_lines(msg: EmailMessage) -> list[str]:
    lines: list[str] = []
    for part in msg.walk():
        kind = part.get_content_type()
        if kind not in ("text/plain", "text/html"):
            continue
        text = part.get_content()
        if kind == "text/html":
            parser = _Lines()
            parser.feed(text)
            lines += parser.lines
        else:
            lines += [line.strip() for line in text.splitlines() if line.strip()]
    return lines


def _sent_at(msg: EmailMessage) -> datetime | None:
    try:
        sent = parsedate_to_datetime(str(msg.get("Date") or ""))
    except (TypeError, ValueError):
        return None
    return sent if sent.tzinfo else sent.replace(tzinfo=UTC)  # "-0000": UTC, zone unknown


def check(msg: EmailMessage, company: str, since: datetime) -> tuple[str | None, str]:
    """(code, "ok") for a Greenhouse security-code email for `company` that arrived at or
    after `since`, else (None, why not). The code stands on a line of its own."""
    if SENDER not in (msg.get("From") or "").lower():
        return None, "not from Greenhouse"
    subject = normalize(msg.get("Subject") or "")
    if "security code" not in subject:
        return None, "not a security code email"
    if not normalize(company) or normalize(company) not in subject:
        return None, f"for another company, not {company}"
    sent = _sent_at(msg)
    if sent is None:
        return None, "no readable Date header"
    if sent < since - CLOCK_SKEW:
        return None, "sent before the Submit click"
    codes = {line for line in _text_lines(msg) if _CODE_LINE.match(line)}
    if len(codes) != 1:
        return None, f"{len(codes)} code-like lines in the body, expected 1"
    return codes.pop(), "ok"


def code_in(msg: EmailMessage, company: str, since: datetime) -> str | None:
    return check(msg, company, since)[0]


def pick_code(
    messages: Iterable[tuple[str, EmailMessage]], company: str, since: datetime, used: set[str]
) -> tuple[str, str] | None:
    """(message id, code) when exactly one new message carries a code, else None."""
    found = [
        (mid, code)
        for mid, msg in messages
        if mid not in used and (code := code_in(msg, company, since))
    ]
    return found[0] if len(found) == 1 else None


def app_password(account: str) -> str | None:
    """The Gmail app password stored by `aja login-mail`, or None when there is none."""
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", account, "-w"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return out.stdout.strip() or None


def save_app_password(account: str, password: str) -> None:
    subprocess.run(
        ["security", "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE, "-a", account,
         "-w", password],
        check=True,
        capture_output=True,
    )  # fmt: skip


class GmailCodes:
    """Waits for one job's code. Call it as `codes(company, clicked_at)`."""

    def __init__(
        self,
        account: str,
        password: str,
        connect: Callable[[], imaplib.IMAP4] = lambda: imaplib.IMAP4_SSL(IMAP_HOST),
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        timeout_s: float = CODE_TIMEOUT_S,
    ):
        self.account, self.password = account, password
        self.connect, self.sleep, self.clock, self.timeout_s = connect, sleep, clock, timeout_s
        self.used: set[str] = set()  # message ids whose code was already typed
        # A company whose code never came: its late code could be read as the next one's.
        self.missed: set[str] = set()

    @contextlib.contextmanager
    def _inbox(self) -> Iterator[imaplib.IMAP4]:
        """Signed in, All Mail (or INBOX) opened read-only."""
        imap = self.connect()
        try:
            imap.login(self.account, self.password)
            status, _ = imap.select(MAILBOX, readonly=True)
            if status != "OK":  # a non-English Gmail names All Mail differently
                imap.select("INBOX", readonly=True)
            yield imap
        finally:
            with contextlib.suppress(Exception):  # already closed
                imap.logout()

    def check(self) -> None:
        """Sign in and open the inbox; raises when Gmail refuses."""
        with self._inbox():
            pass

    def _recent(self, since: datetime) -> list[tuple[str, EmailMessage]]:
        with self._inbox() as imap:
            return self._fetch(imap, since)

    def _fetch(self, imap: imaplib.IMAP4, since: datetime) -> list[tuple[str, EmailMessage]]:
        day = (since - timedelta(days=1)).strftime("%d-%b-%Y")
        _, data = imap.search(None, "FROM", f'"{SENDER}"', "SINCE", day)
        messages = []
        for num in (data[0] or b"").split()[-10:]:
            _, parts = imap.fetch(num, "(BODY.PEEK[])")
            raw = next(p[1] for p in parts if isinstance(p, tuple))
            msg = email.message_from_bytes(raw, policy=email.policy.default)
            messages.append((msg.get("Message-ID") or num.decode(), msg))
        return messages

    def explain(self, company: str, since: datetime) -> list[str]:
        """What the reader sees, one line per recent Greenhouse email (codes masked)."""
        lines = []
        for _, msg in self._recent(since.astimezone(UTC)):
            code, why = check(msg, company, since.astimezone(UTC))
            shown = f"code {code[:2]}******" if code else why
            lines.append(f"{msg.get('Date')} | {msg.get('Subject')} | {shown}")
        return lines or [f"no mail from {SENDER} since {since:%Y-%m-%d}"]

    def __call__(self, company: str, since: datetime) -> str | None:
        """The code, or None after the timeout. Signs in once per wait and polls on that
        connection (NOOP shows new mail); signs in again only after an error."""
        if normalize(company) in self.missed:
            return None
        since = since.astimezone(UTC)
        deadline = self.clock() + self.timeout_s
        with contextlib.ExitStack() as stack:
            imap = None
            while True:
                hit = None
                try:
                    if imap is None:
                        imap = stack.enter_context(self._inbox())
                    else:
                        imap.noop()
                    hit = pick_code(self._fetch(imap, since), company, since, self.used)
                except Exception:  # a network blip or an odd message: retry until the deadline
                    stack.close()
                    imap = None
                if hit:
                    self.used.add(hit[0])
                    return hit[1]
                if self.clock() >= deadline:
                    self.missed.add(normalize(company))
                    return None
                self.sleep(POLL_S)
