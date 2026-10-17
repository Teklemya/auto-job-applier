"""LLM fit check (R8, D9) for jobs that passed the hard filters.

Each PENDING job gets its description fetched once and a verdict recorded once. A deterministic
skills pre-filter settles clear mismatches without an LLM call, and the rest are judged several
jobs per call (D7, D10). Verdicts listed in `settings.fit_skip` mark the job SKIPPED; the score
only orders the queue.
"""

import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from . import jd_fetch
from .llm import LLM, LLMError, QuotaExhausted
from .models import Fit, Job, Status
from .profile import FitVerdict, Profile
from .store import JobStore

# A description shorter than this says too little to rule a job out on keywords alone.
MIN_WORDS_FOR_KEYWORD_SKIP = 150
MAX_JD_CHARS = 6_000  # per job; a batch of 8 stays far under the token limits
_JOB_HEADER = re.compile(r"^### Job (\d+)$", re.M)

SYSTEM = """\
You screen software engineering job postings for one candidate, whose resume is attached.
The candidate is early-career and wants volume: apply to any reasonable software role.

Judge every job given, each on its own. Pick one verdict per job:
- strong: the core stack and the kind of work match the resume.
- ok: some overlap; a reasonable new-grad application.
- stretch: little stack overlap, but still general software engineering the candidate could do.
- mismatch: a different discipline (hardware, firmware, data science or ML research, QA-only,
  IT support, sales engineering), or a hard requirement the resume clearly lacks (years of
  professional experience in a specific niche), or the job requires U.S. citizenship, a
  security clearance, or "U.S. person" (ITAR) status. The candidate has none of those three.

When unsure between two verdicts, pick the more generous one, except for the citizenship,
clearance and U.S. person rule. Score 0-100 for how well the resume matches. matched_keywords:
up to 10 skills that appear in both the resume and the job. reason: one short sentence a busy
person can scan. Return one result per job id."""


class FitAnswer(BaseModel):
    verdict: FitVerdict
    score: int = Field(ge=0, le=100)
    matched_keywords: list[str]
    reason: str


class FitItem(FitAnswer):
    id: int


class FitBatch(BaseModel):
    results: list[FitItem]


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


def job_ids_in(prompt: str) -> list[int]:
    return [int(m) for m in _JOB_HEADER.findall(prompt)]


def llm_verdicts(batch: list[tuple[Job, str]], resume: bytes, model: LLM) -> dict[int, Fit]:
    """One call for the whole batch. Jobs missing from the answer are simply absent."""
    prompt = "\n\n".join(
        f"### Job {job.id}\nCompany: {job.company}\nRole: {job.role}\n"
        f"Location: {job.location}\nDescription:\n{jd[:MAX_JD_CHARS]}"
        for job, jd in batch
    )
    out = model.generate(
        system=SYSTEM,
        prompt=prompt,
        schema=FitBatch,
        attachments=[(resume, "application/pdf")],
    )
    wanted = {job.id for job, _ in batch}
    return {
        r.id: Fit(r.verdict, r.score, r.matched_keywords, r.reason, "llm")
        for r in out.results
        if r.id in wanted
    }


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
    queue: list[tuple[Job, str]] = []

    def apply(job: Job, fit: Fit) -> None:
        if fit.verdict in skip:
            store.set_status(job.id, Status.SKIPPED, f"fit {fit.verdict}: {fit.reason}")
            run.counts["skipped"] += 1

    def record(job: Job, fit: Fit) -> None:
        store.save_fit(job.id, fit)
        run.counts["checked" if fit.verdict else "unchecked"] += 1
        run.results.append((job, fit))
        apply(job, fit)

    def flush() -> None:
        try:
            fits = llm_verdicts(queue, resume, model)
        except QuotaExhausted as e:
            run.stopped = str(e)
            return
        except LLMError:
            fits = {}
        for job, _ in queue:
            if job.id in fits:
                record(job, fits[job.id])
            else:
                run.counts["errors"] += 1  # retried on the next run
        queue.clear()

    for job in store.list(status=Status.PENDING):
        fit = store.get_fit(job.id)
        if fit is not None:
            apply(job, fit)
            continue
        new = run.counts["checked"] + run.counts["unchecked"] + len(queue)
        if limit is not None and new >= limit:
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
                record(job, Fit(None, None, [], str(e), "none"))
                continue
            store.save_description(job.id, jd)
        if (kw := keyword_verdict(profile.skills, jd)) is not None:
            record(job, kw)
            continue
        queue.append((job, jd))
        if len(queue) >= profile.llm.fit_batch_size:
            flush()
            if run.stopped:
                break
    if queue and not run.stopped:
        flush()
    run.counts = +run.counts  # drop zero entries
    return run
