from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.utils import format_datetime

from auto_job_applier import inbox

CLICK = datetime(2026, 10, 24, 6, 21, tzinfo=UTC)


def code_email(company="Affirm", code="Xk7pQ2mZ", at=CLICK + timedelta(seconds=40), sender=None):
    msg = EmailMessage()
    msg["From"] = sender or "Greenhouse <no-reply@us.greenhouse-mail.io>"
    msg["To"] = "jane.doe@example.com"
    msg["Subject"] = f"Security code for your application to {company}"
    msg["Date"] = format_datetime(at)
    msg["Message-ID"] = f"<{company}-{code}@greenhouse>"
    msg.set_content("Copy and paste this code into the security code field.")
    msg.add_alternative(
        "<html><body><h1>Greenhouse Recruiting</h1><p>Copy and paste this code into the "
        f"security code field on your application.</p><h1>{code}</h1>"
        "<p>Greenhouse Software</p></body></html>",
        subtype="html",
    )
    return msg


def test_the_code_on_its_own_line_is_read():
    assert inbox.code_in(code_email(), "Affirm", CLICK) == "Xk7pQ2mZ"


def test_a_code_for_another_company_is_ignored():
    assert inbox.code_in(code_email(company="Brex"), "Affirm", CLICK) is None


def test_a_code_sent_before_the_click_is_ignored():
    early = code_email(at=CLICK - timedelta(minutes=5))
    assert inbox.code_in(early, "Affirm", CLICK) is None


def test_only_greenhouse_can_send_a_code():
    fake = code_email(sender="Greenhouse <no-reply@greenhouse-mail.example.com>")
    assert inbox.code_in(fake, "Affirm", CLICK) is None


def test_two_new_codes_for_one_company_are_too_unclear_to_use():
    msgs = [("a", code_email(code="AAAA1111")), ("b", code_email(code="BBBB2222"))]
    assert inbox.pick_code(msgs, "Affirm", CLICK, set()) is None


def test_a_code_already_typed_is_never_used_again():
    msgs = [("a", code_email(code="AAAA1111")), ("b", code_email(code="BBBB2222"))]
    assert inbox.pick_code(msgs, "Affirm", CLICK, {"a"}) == ("b", "BBBB2222")


class FakeIMAP:
    """An inbox whose messages arrive on the given poll (0 = first)."""

    def __init__(self, arrivals):
        self.arrivals, self.polls = arrivals, 0

    def __call__(self):
        self.polls += 1
        return self

    def login(self, user, password):
        assert password == "app-password"

    def select(self, box, readonly):
        assert readonly

    def search(self, charset, *criteria):
        assert inbox.SENDER in " ".join(criteria)
        n = sum(1 for when, _ in self.arrivals if when < self.polls)
        return "OK", [b" ".join(str(i + 1).encode() for i in range(n))]

    def fetch(self, num, what):
        assert "PEEK" in what  # never marks mail as read
        msg = self.arrivals[int(num) - 1][1]
        return "OK", [(b"1 (BODY[] {1})", msg.as_bytes()), b")"]

    def logout(self):
        pass


def reader(arrivals, timeout_s=60):
    now = [0.0]

    def sleep(s):
        now[0] += s

    return inbox.GmailCodes(
        "jane.doe@example.com",
        "app-password",
        connect=FakeIMAP(arrivals),
        sleep=sleep,
        clock=lambda: now[0],
        timeout_s=timeout_s,
    )


def test_the_reader_waits_for_a_late_code():
    codes = reader([(3, code_email())])
    assert codes("Affirm", CLICK) == "Xk7pQ2mZ"


def test_back_to_back_jobs_at_one_company_each_get_their_own_code():
    first = code_email(code="AAAA1111", at=CLICK + timedelta(seconds=30))
    second = code_email(code="BBBB2222", at=CLICK + timedelta(minutes=2))
    codes = reader([(0, first), (2, second)])
    assert codes("Affirm", CLICK) == "AAAA1111"
    assert codes("Affirm", CLICK + timedelta(minutes=1)) == "BBBB2222"


def test_no_code_in_time_is_none_and_that_company_waits_for_the_next_run():
    codes = reader([(99, code_email())], timeout_s=30)
    assert codes("Affirm", CLICK) is None
    # Its code could still arrive and be mistaken for the next Affirm job's.
    assert codes("Affirm", CLICK + timedelta(minutes=5)) is None
