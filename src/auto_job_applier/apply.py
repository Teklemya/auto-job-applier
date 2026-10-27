"""Turn an open application form into an answer packet and decide if it may be submitted (D1).

Engines read the page; this module answers it. The packet is stored with every draft, so
`aja review` can re-fill the form without the LLM (D3).
"""

import contextlib
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Protocol

from .answers.bank import AnswerBank
from .answers.resolver import Writer, acknowledges_only, gate, resolve
from .engines.base import REJECTED, Action, Packet, PageField, SubmitResult, fill_plan
from .llm import LLM
from .models import Job, ResolvedAnswer, Status
from .profile import Profile
from .store import JobStore

NO_FORM = "no application form found on the page"
SHADOW_READY = "shadow mode: every required field answered, not submitted"
# Every outcome after a click on Submit starts with CLICKED: the daily cap counts them all, and
# a draft made after a click is never submitted again automatically.
CLICKED = "submit clicked: "
AUTO_SUBMITTED = CLICKED + "confirmation page seen"
UNCONFIRMED = CLICKED + "no confirmation page; check your email before applying again"
EMPTY_ON_PAGE = "required but empty on the page: "
BY_OWNER = CLICKED + "submitted by the owner, confirmation page seen"
OWNER_UNCLEAR = CLICKED + "the owner left the form without a confirmation page; check your email"

MAX_PASSES = 3  # fill rounds while answers keep revealing new fields


class FormSession(Protocol):
    """An application form open in the browser, as an engine exposes it."""

    def fields(self) -> list[PageField]: ...

    def fill(self, plan: list[Action]) -> list[str]:
        """Carry out the plan; return the DOM ids the page wouldn't take."""
        ...

    def page_blockers(self) -> list[str]:
        """Problems on the page itself, e.g. a visible CAPTCHA."""
        ...

    def missing_required(self) -> list[tuple[str, str, str]]:
        """(dom id, label, kind) of required inputs the page still shows empty, including ones
        the field reader never saw (a checkbox without a plain label)."""
        ...

    def tick(self, dom_id: str) -> bool:
        """Tick a checkbox; False when the page won't take it."""
        ...

    def highlight(self, dom_ids: list[str]) -> None: ...

    def wait_for_owner(self, wait_ms: int) -> SubmitResult:
        """Let the owner finish and submit the form. Confirmed, or problems=[] when the form is
        still there unsent, or a problem when it was left without a confirmation."""
        ...

    def screenshot(self, path: Path) -> None: ...

    # Type-to-search dropdowns: the option each box got, and the options seen while typing.
    picked: dict[str, str]
    offered: dict[str, list[str]]

    def submit(self) -> SubmitResult:
        """Click Submit and wait for the confirmation page, an error, or nothing."""
        ...


def build_packet(
    fields: list[PageField],
    profile: Profile,
    *,
    bank: AnswerBank,
    llm: LLM,
    job: Job,
    jd: str,
    writer: Writer | None = None,
) -> Packet:
    if not fields:
        return Packet([], [], [NO_FORM])
    answers = resolve(
        [pf.field for pf in fields], profile, bank=bank, llm=llm, job=job, jd=jd, writer=writer
    )
    return Packet(fields, answers, gate(answers))


def stage(
    job: Job,
    session: FormSession,
    *,
    store: JobStore,
    profile: Profile,
    llm: LLM,
    jd: str,
    shots_dir: Path,
    writer: Writer | None = None,
    submit: bool = False,
    held: str = SHADOW_READY,
    hand_over_ms: int = 0,
    on_hand_over: Callable[[list[str]], None] = lambda blockers: None,
) -> Packet:
    """Answer and fill the form, then submit it or save it as a draft (D1, D3).

    It submits only when `submit` is set (live mode with the daily cap not reached; the caller
    decides) and nothing blocks; `held` says why a ready form was not submitted. APPLIED
    needs the confirmation page (S3). A page that rejects the submit stages a draft; no
    confirmation at all is FAILED, so the job is never re-sent. With `hand_over_ms` (the owner
    is watching), a blocked form is left open for the owner to finish and submit instead.

    Answers can change the form: "Hispanic/Latino: No" reveals a race question, "Current
    role" hides the end date. So the page is read again after each pass, new fields are
    answered in another pass, and fields that went away are dropped.
    """
    assert job.id is not None
    bank = AnswerBank(store)
    on_page = session.fields()
    fields: list[PageField] = []
    answers: list[ResolvedAnswer] = []
    rejected: set[str] = set()
    for _ in range(MAX_PASSES):
        known = {pf.dom_id for pf in fields}
        new = [pf for pf in on_page if pf.dom_id not in known]
        if not new:
            break
        part = build_packet(new, profile, bank=bank, llm=llm, job=job, jd=jd, writer=writer)
        rejected |= set(session.fill(fill_plan(part.fields, part.answers)))
        fields += part.fields
        answers += part.answers
        on_page = session.fields()
    showing = {pf.dom_id for pf in on_page}
    kept = [(pf, a) for pf, a in zip(fields, answers, strict=True) if pf.dom_id in showing]
    packet = Packet(*_with_typeahead(kept, session), [])
    packet.blockers = gate(packet.answers) if packet.fields else [NO_FORM]
    packet.blockers += [
        f"could not fill: {pf.field.label}"
        for pf in packet.fields
        if pf.dom_id in rejected and pf.field.required
    ]
    packet.blockers += session.page_blockers()
    # Checked on the page itself before Submit: a field the reader missed is still a blocker.
    flagged = rejected | {
        pf.dom_id
        for pf, a in zip(packet.fields, packet.answers, strict=True)
        if pf.field.required and (not a.confident or a.value is None)
    }
    empty = _empty_after_acknowledging(session, profile)
    packet.blockers += [
        f"{EMPTY_ON_PAGE}{label}" for dom_id, label in empty if dom_id not in flagged
    ]
    shots_dir.mkdir(parents=True, exist_ok=True)
    session.screenshot(shots_dir / f"{job.id}-{datetime.now():%Y%m%d-%H%M%S}.png")
    store.save_packet(job.id, packet)
    if packet.blockers and submit and hand_over_ms:
        return _hand_over(job, session, packet, store, flagged, empty, hand_over_ms, on_hand_over)
    if packet.blockers or not submit:
        store.set_status(job.id, Status.STAGED, _draft_reason(packet, held))
        return packet
    try:
        result = session.submit()
    except Exception as e:  # the click may have gone through: never retried automatically
        store.set_status(job.id, Status.FAILED, f"{UNCONFIRMED} (browser error: {e})"[:300])
        return packet
    # The status is written before anything else can fail, so a sent job is never re-queued.
    if result.confirmed:
        store.set_status(job.id, Status.APPLIED, AUTO_SUBMITTED)
    elif result.problems:
        packet.blockers = result.problems
        store.save_packet(job.id, packet)
        not_sent = all(p.startswith(REJECTED) for p in result.problems)
        store.set_status(
            job.id, Status.STAGED, ("" if not_sent else CLICKED) + _draft_reason(packet)
        )
    else:
        store.set_status(job.id, Status.FAILED, UNCONFIRMED)
    with contextlib.suppress(Exception):
        session.screenshot(shots_dir / f"{job.id}-{datetime.now():%Y%m%d-%H%M%S}-submit.png")
    return packet


def _empty_after_acknowledging(session: FormSession, profile: Profile) -> list[tuple[str, str]]:
    """Required inputs still empty on the page, after ticking the acknowledgement boxes."""
    try:
        empty = session.missing_required()
    except Exception:  # can't tell: never submit on a guess
        return [("", "could not check the page for empty fields")]
    left = []
    for dom_id, label, kind in empty:
        ack = kind == "checkbox" and acknowledges_only(label)
        if not (ack and profile.preferences.tick_acknowledgements and session.tick(dom_id)):
            left.append((dom_id, label))
    return left


def _hand_over(
    job: Job,
    session: FormSession,
    packet: Packet,
    store: JobStore,
    flagged: set[str],
    empty: list[tuple[str, str]],
    wait_ms: int,
    on_hand_over: Callable[[list[str]], None],
) -> Packet:
    """The owner finishes the outlined fields and clicks Submit; the page is watched for the
    confirmation, as after the tool's own click."""
    assert job.id is not None
    store.set_status(job.id, Status.STAGED, _draft_reason(packet))  # in case the wait dies
    with contextlib.suppress(Exception):
        session.highlight(sorted(flagged | {dom_id for dom_id, _ in empty if dom_id}))
    on_hand_over(packet.blockers)
    result = session.wait_for_owner(wait_ms)
    if result.confirmed:
        store.set_status(job.id, Status.APPLIED, BY_OWNER)
    elif result.problems:
        store.set_status(job.id, Status.FAILED, OWNER_UNCLEAR)
    return packet


def _with_typeahead(
    kept: list[tuple[PageField, ResolvedAnswer]], session: FormSession
) -> tuple[list[PageField], list[ResolvedAnswer]]:
    """Record what the type-to-search boxes did: the option picked becomes the answer, and the
    options seen go on the field, so `aja review` can number them when none fit."""
    fields, answers = [], []
    for pf, a in kept:
        seen = session.picked.get(pf.dom_id), session.offered.get(pf.dom_id)
        if seen[1] and not pf.field.options:
            pf = PageField(pf.dom_id, replace(pf.field, options=seen[1], typeahead=True))
            a = replace(a, field=pf.field)
        if seen[0]:
            a = replace(a, value=seen[0], alternatives=[], reason="picked on the page")
        fields.append(pf)
        answers.append(a)
    return fields, answers


def _draft_reason(packet: Packet, held: str = SHADOW_READY) -> str:
    return f"needs review: {len(packet.blockers)} blocking" if packet.blockers else held
