"""Screening: hard filters (R4–R6), dedupe (R7), and recording every outcome (R9).

The LLM fit check (R8) slots in after these rules in T4.
"""

import re
from collections import Counter
from dataclasses import dataclass
from urllib.parse import urlsplit

from .models import ATS, Job, Status, Tier
from .profile import Targeting
from .store import JobStore

__all__ = ["Tier", "Verdict", "hard_filter", "screen_new", "dedupe_key"]

_SENIOR = re.compile(
    r"\b(senior|sr\.?|staff|principal|lead|manager|director|architect|head of|distinguished)\b",
    re.I,
)
_HIGH_LEVEL = re.compile(r"\b(engineer|developer|sde|swe)\s+(iii|iv|v|[3-9])\b", re.I)
_EARLY_CAREER = re.compile(
    r"new grad|new college grad|entry[- ]level|early career|junior|university grad", re.I
)
_NON_US = re.compile(
    r"\b(canada|uk|united kingdom|london|india|germany|ireland|mexico|brazil|poland|israel|"
    r"singapore|japan|china|australia|netherlands|france|spain|toronto|vancouver|bc|ontario|"
    r"europe|emea|apac|taiwan|korea)\b",
    re.I,
)
_US_UNSPECIFIED = {"united states", "usa", "us"}
_FLAG_SKIPS = {
    "no_sponsorship": "no sponsorship (🛂)",
    "us_citizenship": "citizenship required (🇺🇸)",
}
# Statuses that claim a company+role, so later postings of it are duplicates.
_CLAIMED = (Status.PENDING, Status.STAGED, Status.APPLIED, Status.FAILED, Status.MANUAL)


@dataclass(frozen=True)
class Verdict:
    status: Status
    reason: str | None = None
    tier: Tier | None = None


def _matches_any(text: str, names: list[str]) -> bool:
    return any(re.search(rf"(?<!\w){re.escape(n)}(?!\w)", text, re.I) for n in names)


def _location_tier(job: Job, t: Targeting) -> Tier | None:
    locations = [loc for loc in job.location.split("; ") if loc]
    is_big = "faang" in job.flags or job.company.lower() in {c.lower() for c in t.big_companies}
    tiers = []
    for loc in locations:
        if _NON_US.search(loc):
            continue
        if "remote" in loc.lower():
            tiers.append(Tier.REMOTE)
        elif _matches_any(loc, t.kc_metro):
            tiers.append(Tier.KC_METRO)
        elif is_big and _matches_any(loc, t.hubs):
            tiers.append(Tier.BIG_HUB)
        elif loc.strip().lower() in _US_UNSPECIFIED:
            tiers.append(Tier.US_UNSPECIFIED)
    # Workday often encodes remote only in the URL, e.g. ".../job/Remote-California/..."
    all_us = locations and not any(_NON_US.search(loc) for loc in locations)
    if all_us and "remote" in urlsplit(job.url).path.lower():
        tiers.append(Tier.REMOTE)
    return min(tiers) if tiers else None


def hard_filter(job: Job, t: Targeting) -> Verdict:
    for flag, reason in _FLAG_SKIPS.items():
        if flag in job.flags:
            return Verdict(Status.SKIPPED, reason)
    if not _EARLY_CAREER.search(job.role) and (
        _SENIOR.search(job.role) or _HIGH_LEVEL.search(job.role)
    ):
        return Verdict(Status.SKIPPED, "senior title")
    tier = _location_tier(job, t)
    if tier is None:
        return Verdict(Status.SKIPPED, f"location not targeted: {job.location or 'unknown'}")
    if job.ats is ATS.MANUAL:
        return Verdict(Status.MANUAL, "unsupported site: apply manually", tier)
    return Verdict(Status.PENDING, None, tier)


def dedupe_key(job: Job) -> tuple[str, str]:
    company = re.sub(r"[^a-z0-9]", "", job.company.lower())
    title = " ".join(re.sub(r"[^a-z0-9]+", " ", job.role.lower()).split())
    return company, title


def screen_new(store: JobStore, t: Targeting) -> dict[Status, int]:
    """Screen every NEW job. Returns counts per resulting status."""
    claimed = {dedupe_key(j): j.id for s in _CLAIMED for j in store.list(status=s)}
    counts: Counter[Status] = Counter()
    for job in store.list(status=Status.NEW):
        v = hard_filter(job, t)
        if v.status in (Status.PENDING, Status.MANUAL):
            key = dedupe_key(job)
            if key in claimed:
                v = Verdict(Status.SKIPPED, f"duplicate of #{claimed[key]}", v.tier)
            else:
                claimed[key] = job.id
        store.set_status(job.id, v.status, v.reason, tier=v.tier)
        counts[v.status] += 1
    return dict(counts)
