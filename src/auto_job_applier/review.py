"""Owner review of a staged draft (R17): answer what blocked it, re-fill, submit by hand.

Answers given here go into the answer bank, so the same question is answered next time (R10).
"""

from dataclasses import replace

from .answers.bank import AnswerBank
from .answers.resolver import is_work_auth, match_option
from .engines.base import Packet, PageField
from .models import ResolvedAnswer

_UNFILLED = "could not fill: "


def _blocking_labels(packet: Packet) -> set[str]:
    return {b.removeprefix(_UNFILLED) for b in packet.blockers}


def blocking_fields(packet: Packet) -> list[PageField]:
    """Fields to ask the owner about, in page order. Page problems (CAPTCHA) aren't fields."""
    labels = _blocking_labels(packet)
    return [pf for pf in packet.fields if pf.field.label in labels]


def apply_answers(packet: Packet, given: dict[str, str], bank: AnswerBank) -> Packet:
    """Put the owner's answers (by DOM id) into the packet and the bank; clear their blockers."""
    answers = list(packet.answers)
    answered = set()
    for i, pf in enumerate(packet.fields):
        if pf.dom_id not in given:
            continue
        value = match_option(given[pf.dom_id], pf.field)
        if value is None:
            raise ValueError(f"{pf.field.label}: choose one of {', '.join(pf.field.options)}")
        answers[i] = ResolvedAnswer(pf.field, value, "owner", True, "answered at review")
        if not is_work_auth(pf.field.label):  # the bank never answers these (R11)
            bank.save(pf.field.label, value)
        answered.add(pf.field.label)
    blockers = [b for b in packet.blockers if b.removeprefix(_UNFILLED) not in answered]
    return replace(packet, answers=answers, blockers=blockers)
