"""What every ATS engine shares: page fields tied to their DOM ids, and the fill plan.

Engines only navigate. They read the visible form into `PageField`s, the resolver answers the
`FormField`s, and `fill_plan` turns the answers into browser actions.
"""

from dataclasses import dataclass
from pathlib import Path

from ..models import FormField, ResolvedAnswer


@dataclass
class PageField:
    dom_id: str
    field: FormField


@dataclass(frozen=True)
class Action:
    kind: str  # type | choose | upload | paste | check
    dom_id: str
    value: str


_KIND_ACTION = {"text": "type", "textarea": "type", "select": "choose"}


def fill_plan(fields: list[PageField], answers: list[ResolvedAnswer]) -> list[Action]:
    """Browser actions for the confident answers. Everything else stays blank (D1)."""
    if len(fields) != len(answers):
        raise ValueError(f"{len(fields)} fields but {len(answers)} answers")
    plan = []
    for pf, a in zip(fields, answers, strict=True):
        if a.value is None or a.value == "" or not a.confident:
            continue
        kind = pf.field.kind
        if kind == "checkbox":
            if a.value.strip().lower() in ("yes", "true"):
                plan.append(Action("check", pf.dom_id, ""))
        elif kind == "file":
            # The writer returns cover letters as text; paste it when it isn't a file on disk.
            action = "upload" if Path(a.value).is_file() else "paste"
            plan.append(Action(action, pf.dom_id, a.value))
        else:
            plan.append(Action(_KIND_ACTION.get(kind, "type"), pf.dom_id, a.value))
    return plan


@dataclass
class Packet:
    """Everything needed to re-fill a draft without the LLM (D3), and why it was staged."""

    fields: list[PageField]
    answers: list[ResolvedAnswer]
    blockers: list[str]  # labels of required fields without a confident answer, or page issues
