"""Screening: hard filters (R4–R6), dedupe (R7), and recording every outcome (R9).

The LLM fit check (R8) slots in after these rules in T4.
"""

import re
from collections import Counter
from dataclasses import dataclass

from . import geo
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
_FLAG_SKIPS = {
    "no_sponsorship": "no sponsorship (🛂)",
    "us_citizenship": "citizenship required (🇺🇸)",
    "advanced_degree": "advanced degree required (🎓)",
}
# Statuses that claim a company+role, so later postings of it are duplicates.
_CLAIMED = (Status.PENDING, Status.STAGED, Status.APPLIED, Status.FAILED, Status.MANUAL)
_DUPLICATE_OF = re.compile(r"^duplicate of #(\d+)$")


@dataclass(frozen=True)
class Verdict:
    status: Status
    reason: str | None = None
    tier: Tier | None = None


def _location_tier(job: Job, t: Targeting) -> Tier | None:
    """Best tier across the job's locations; None if no location is targeted."""
    tiers = []
    for loc in filter(None, job.location.split("; ")):
        if geo.is_remote_us(loc):
            tiers.append(Tier.REMOTE)
        elif any(geo.place_matches(loc, place) for place in t.kc_metro):
            tiers.append(Tier.KC_METRO)
        elif any(geo.place_matches(loc, place) for place in t.hubs):
            tiers.append(Tier.HUB)
        elif geo.is_us_country(loc):
            tiers.append(Tier.US_UNSPECIFIED)
    # Simplify sometimes lists only a state ("California") for Workday remote roles.
    if job.ats is ATS.WORKDAY and geo.workday_remote_us(job.url):
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


def _release_orphaned_duplicates(store: JobStore) -> None:
    """Reopen duplicates whose owner no longer claims the role (closed or screened out),
    so the remaining posting isn't lost."""
    claiming = {j.id for s in _CLAIMED for j in store.list(status=s)}
    for job in store.list(status=Status.SKIPPED):
        m = _DUPLICATE_OF.match(job.reason or "")
        if m and int(m.group(1)) not in claiming:
            store.set_status(job.id, Status.NEW, f"owner #{m.group(1)} no longer active: re-screen")


def screen_new(store: JobStore, t: Targeting) -> dict[Status, int]:
    """Screen every NEW job. Returns counts per resulting status.

    One company+role is applied to once. A fillable posting takes the role over from a MANUAL
    (unsupported site) one; otherwise the first claim wins.
    """
    _release_orphaned_duplicates(store)
    claimed = {dedupe_key(j): (j.id, j.status) for s in _CLAIMED for j in store.list(status=s)}
    counts: Counter[Status] = Counter()
    screened_now: set[int] = set()
    for job in store.list(status=Status.NEW):
        v = hard_filter(job, t)
        if v.status in (Status.PENDING, Status.MANUAL):
            key = dedupe_key(job)
            if key not in claimed:
                claimed[key] = (job.id, v.status)
            else:
                owner_id, owner_status = claimed[key]
                if owner_status is Status.MANUAL and v.status is Status.PENDING:
                    store.set_status(owner_id, Status.SKIPPED, f"duplicate of #{job.id}")
                    if owner_id in screened_now:
                        counts[Status.MANUAL] -= 1
                        counts[Status.SKIPPED] += 1
                    claimed[key] = (job.id, v.status)
                else:
                    v = Verdict(Status.SKIPPED, f"duplicate of #{owner_id}", v.tier)
        store.set_status(job.id, v.status, v.reason, tier=v.tier)
        counts[v.status] += 1
        screened_now.add(job.id)
    return {s: n for s, n in counts.items() if n}
