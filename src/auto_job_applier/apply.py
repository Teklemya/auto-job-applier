"""Turn an open application form into an answer packet and decide if it may be submitted (D1).

Engines read the page; this module answers it. The packet is stored with every draft, so
`aja review` can re-fill the form without the LLM (D3).
"""

from datetime import datetime
from pathlib import Path
from typing import Protocol

from .answers.bank import AnswerBank
from .answers.resolver import Writer, gate, resolve
from .engines.base import Action, Packet, PageField, SubmitResult, fill_plan
from .llm import LLM
from .models import Job, ResolvedAnswer, Status
from .profile import Profile
from .store import JobStore

NO_FORM = "no application form found on the page"
SHADOW_READY = "shadow mode: every required field answered, not submitted"
AUTO_SUBMITTED = "submitted automatically, confirmation page seen"
UNCONFIRMED = "clicked Submit but saw no confirmation page: check your email before applying again"
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

    def screenshot(self, path: Path) -> None: ...

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
) -> Packet:
    """Answer and fill the form, then submit it or save it as a draft (D1, D3).

    It submits only when `submit` is set (live mode with the daily cap not reached; the caller
    decides) and nothing blocks. APPLIED needs the confirmation page (S3). A page that rejects
    the submit stages a draft; no confirmation at all is FAILED, so the job is never re-sent.

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
    packet = Packet([pf for pf, _ in kept], [a for _, a in kept], [])
    packet.blockers = gate(packet.answers) if packet.fields else [NO_FORM]
    packet.blockers += [
        f"could not fill: {pf.field.label}"
        for pf in packet.fields
        if pf.dom_id in rejected and pf.field.required
    ]
    packet.blockers += session.page_blockers()
    shots_dir.mkdir(parents=True, exist_ok=True)
    session.screenshot(shots_dir / f"{job.id}-{datetime.now():%Y%m%d-%H%M%S}.png")
    store.save_packet(job.id, packet)
    if packet.blockers or not submit:
        store.set_status(job.id, Status.STAGED, _draft_reason(packet))
        return packet
    result = session.submit()
    session.screenshot(shots_dir / f"{job.id}-{datetime.now():%Y%m%d-%H%M%S}-submit.png")
    if result.confirmed:
        store.set_status(job.id, Status.APPLIED, AUTO_SUBMITTED)
    elif result.problems:
        packet.blockers = result.problems
        store.save_packet(job.id, packet)
        store.set_status(job.id, Status.STAGED, _draft_reason(packet))
    else:
        store.set_status(job.id, Status.FAILED, UNCONFIRMED)
    return packet


def _draft_reason(packet: Packet) -> str:
    return f"needs review: {len(packet.blockers)} blocking" if packet.blockers else SHADOW_READY
