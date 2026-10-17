"""LLM fit check (R8, D9) for jobs that passed the hard filters.

Each PENDING job gets its description fetched once and a verdict recorded once. A deterministic
skills pre-filter settles clear mismatches without an LLM call (D7). Verdicts listed in
`settings.fit_skip` mark the job SKIPPED; the score only orders the queue.
"""

import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from . import jd_fetch
from .llm import LLM, QuotaExhausted
from .models import Fit, Job, Status
from .profile import FitVerdict, Profile
from .store import JobStore

# A description shorter than this says too little to rule a job out on keywords alone.
MIN_WORDS_FOR_KEYWORD_SKIP = 150
MAX_JD_CHARS = 15_000

SYSTEM = """\
You screen software engineering job postings for one candidate, whose resume is attached.
The candidate is early-career and wants volume: apply to any reasonable software role.

Pick one verdict:
- strong: the core stack and the kind of work match the resume.
- ok: some overlap; a reasonable new-grad application.
- stretch: little stack overlap, but still general software engineering the candidate could do.
- mismatch: a different discipline (hardware, firmware, data science or ML research, QA-only,
  IT support, sales engineering) or a hard requirement the resume clearly lacks
  (e.g. an active security clearance, years of professional experience in a specific niche).

When unsure between two verdicts, pick the more generous one. Score 0-100 for how well the
resume matches. matched_keywords: up to 10 skills that appear in both the resume and the job.
reason: one short sentence a busy person can scan."""


class FitAnswer(BaseModel):
    verdict: FitVerdict
    score: int = Field(ge=0, le=100)
    matched_keywords: list[str]
    reason: str


@dataclass
class FitRun:
    counts: Counter[str] = field(default_factory=Counter)
    results: list[tuple[Job, Fit]] = field(default_factory=list)
    stopped: str | None = None  # why the run ended early (quota)


def _skill_pattern(skill: str) -> re.Pattern[str]:
    # Word boundaries that still work for "C++", "C#", ".NET", "Node.js" ("Go" ≠ "Golang",
    # but "C++17" is C++).
    tail = r"(?!\w)" if skill[-1:].isalnum() else ""
    return re.compile(rf"(?<![\w.+#]){re.escape(skill)}{tail}", re.I)


def matched_skills(skills: list[str], text: str) -> list[str]:
    return [s for s in skills if _skill_pattern(s).search(text)]


def keyword_verdict(skills: list[str], jd: str) -> Fit | None:
    """`mismatch` when a substantial description mentions none of the skills; else None."""
    if not skills or len(jd.split()) < MIN_WORDS_FOR_KEYWORD_SKIP or matched_skills(skills, jd):
        return None
    return Fit("mismatch", 0, [], "none of the resume skills appear in the description", "keyword")


def llm_verdict(job: Job, jd: str, resume: bytes, model: LLM) -> Fit:
    prompt = (
        f"Company: {job.company}\nRole: {job.role}\nLocation: {job.location}\n\n"
        f"Job description:\n{jd[:MAX_JD_CHARS]}"
    )
    a = model.generate(
        system=SYSTEM,
        prompt=prompt,
        schema=FitAnswer,
        attachments=[(resume, "application/pdf")],
    )
    return Fit(a.verdict, a.score, a.matched_keywords, a.reason, "llm")


def screen_fit(
    store: JobStore,
    profile: Profile,
    model: LLM,
    fetch: Callable[[Job], str] = jd_fetch.fetch_description,
    limit: int | None = None,
) -> FitRun:
    """Fit-check PENDING jobs. `limit` caps how many new checks run."""
    run = FitRun()
    skip: set[FitVerdict] = set(profile.settings.fit_skip)
    resume = profile.resume_path.read_bytes()
    for job in store.list(status=Status.PENDING):
        fit = store.get_fit(job.id)
        if fit is None:
            if limit is not None and run.counts["checked"] + run.counts["unchecked"] >= limit:
                break
            jd = store.get_description(job.id)
            if jd is None:
                try:
                    jd = fetch(job)
                except jd_fetch.PostingClosed as e:
                    store.set_status(job.id, Status.SKIPPED, f"posting closed: {e}")
                    run.counts["closed"] += 1
                    continue
                except jd_fetch.FetchError:
                    run.counts["errors"] += 1
                    continue
                except jd_fetch.Unsupported as e:
                    fit = Fit(None, None, [], str(e), "none")
                else:
                    store.save_description(job.id, jd)
            if fit is None:
                try:
                    fit = keyword_verdict(profile.skills, jd) or llm_verdict(job, jd, resume, model)
                except QuotaExhausted as e:
                    run.stopped = str(e)
                    break
            store.save_fit(job.id, fit)
            run.counts["checked" if fit.verdict else "unchecked"] += 1
            run.results.append((job, fit))
        if fit.verdict in skip:
            store.set_status(job.id, Status.SKIPPED, f"fit {fit.verdict}: {fit.reason}")
            run.counts["skipped"] += 1
    run.counts = +run.counts  # drop zero entries
    return run
