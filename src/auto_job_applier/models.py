"""Domain types shared across collection, screening, storage and the ATS engines."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class ATS(StrEnum):
    WORKDAY = "workday"
    GREENHOUSE = "greenhouse"
    LEVER = "lever"
    MANUAL = "manual"  # any site v1 can't fill; listed for the owner to apply by hand


class Tier(StrEnum):
    """Location priority (R6), best first. Values sort in priority order."""

    REMOTE = "1-remote"
    KC_METRO = "2-kc-metro"
    HUB = "3-hub"  # any company in a US tech hub
    US_UNSPECIFIED = "4-us-unspecified"  # bare "United States": location unknown


class Status(StrEnum):
    NEW = "NEW"  # collected, not screened yet
    SKIPPED = "SKIPPED"  # filtered out; `reason` says why
    PENDING = "PENDING"  # passed screening, queued to apply
    STAGED = "STAGED"  # draft: waiting on the owner (blocking fields, CAPTCHA, shadow mode)
    APPLIED = "APPLIED"  # submitted and confirmation page seen
    FAILED = "FAILED"  # engine error; `reason` says why
    MANUAL = "MANUAL"  # fits, but on an unsupported site


@dataclass
class Job:
    url: str
    company: str
    role: str
    location: str = ""
    ats: ATS = ATS.MANUAL
    status: Status = Status.NEW
    flags: list[str] = field(default_factory=list)  # e.g. "no_sponsorship", "faang"
    age: str = ""  # as listed by the source, e.g. "3d"
    source: str = ""  # "simplify" | "jobright"
    reason: str | None = None  # why SKIPPED / STAGED / FAILED
    tier: Tier | None = None  # set by screening
    id: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass
class FormField:
    """One visible form field, as any ATS engine extracts it."""

    label: str
    kind: str = "text"  # text | textarea | select | radio | checkbox | file
    required: bool = False
    options: list[str] = field(default_factory=list)  # select/radio choices


@dataclass
class ResolvedAnswer:
    field: FormField
    value: str | None  # None = leave blank; option fields hold the exact option text
    source: str  # profile | preferences | bank | llm | none
    confident: bool
    reason: str = ""


@dataclass
class Fit:
    """LLM fit check result (R8, D9). Recorded once per job."""

    verdict: str | None  # strong | ok | stretch | mismatch; None = not checked (no description)
    score: int | None  # 0-100; orders the queue, never decides a skip
    matched_keywords: list[str]
    reason: str
    source: str  # "llm" | "keyword" (pre-filter, no LLM call) | "none"
