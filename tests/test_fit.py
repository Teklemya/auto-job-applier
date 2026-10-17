import pytest

from auto_job_applier import fit, jd_fetch, llm
from auto_job_applier.models import ATS, Fit, Job, Status, Tier
from auto_job_applier.profile import load_profile
from auto_job_applier.store import JobStore

JD_PYTHON = (
    "We build backend services in Python and Go on AWS. " + "Collaborate with the team. " * 60
)


@pytest.fixture
def store(tmp_path):
    s = JobStore(tmp_path / "jobs.db")
    yield s
    s.close()


@pytest.fixture
def profile(example_profile_data, write_profile):
    return load_profile(write_profile(example_profile_data))


class FakeLLM:
    """Each queued answer is an Exception (raised) or a FitAnswer given to every job id the
    batched prompt contains."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def generate(self, **kw):
        self.calls.append(kw)
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        ids = fit.job_ids_in(kw["prompt"])
        return fit.FitBatch(results=[fit.FitItem(id=i, **a.model_dump()) for i in ids])


def answer(verdict="ok", score=60, reason="decent overlap"):
    return fit.FitAnswer(verdict=verdict, score=score, matched_keywords=["Python"], reason=reason)


def pending(store, n=1):
    jobs = [
        Job(
            url=f"https://job-boards.greenhouse.io/acme/jobs/{i}",
            company=f"Acme{i}",
            role="Software Engineer",
            location="Remote",
            ats=ATS.GREENHOUSE,
            source="simplify",
        )
        for i in range(n)
    ]
    store.upsert_many(jobs)
    for j in store.list():
        store.set_status(j.id, Status.PENDING, tier=Tier.REMOTE)
    return store.list(status=Status.PENDING)


# --- screen_fit ---


def test_ok_fit_stays_pending_and_is_recorded(store, profile):
    [job] = pending(store)
    model = FakeLLM(answer("ok", 62))
    run = fit.screen_fit(store, profile, model, fetch=lambda j: JD_PYTHON)
    assert run.counts == {"checked": 1}
    assert store.get(job.id).status is Status.PENDING
    assert store.get_fit(job.id) == Fit("ok", 62, ["Python"], "decent overlap", "llm")
    assert store.get_description(job.id) == JD_PYTHON
    # The resume PDF goes to the model, and the prompt names the job
    [call] = model.calls
    assert call["attachments"] == [(profile.resume_path.read_bytes(), "application/pdf")]
    assert "Acme0" in call["prompt"] and JD_PYTHON[:40] in call["prompt"]
    assert call["schema"] is fit.FitBatch


def test_mismatch_is_skipped_with_reason(store, profile):
    [job] = pending(store)
    run = fit.screen_fit(
        store,
        profile,
        FakeLLM(answer("mismatch", 10, "data science role")),
        fetch=lambda j: JD_PYTHON,
    )
    assert run.counts == {"checked": 1, "skipped": 1}
    got = store.get(job.id)
    assert got.status is Status.SKIPPED
    assert got.reason == "fit mismatch: data science role"
    assert got.tier is Tier.REMOTE


def test_fit_skip_is_configurable(store, profile):
    profile.settings.fit_skip = ["mismatch", "stretch"]
    [job] = pending(store)
    fit.screen_fit(store, profile, FakeLLM(answer("stretch", 30)), fetch=lambda j: JD_PYTHON)
    assert store.get(job.id).status is Status.SKIPPED


def test_fit_runs_at_most_once_per_job(store, profile):
    [job] = pending(store)
    model = FakeLLM(answer("ok"))
    fit.screen_fit(store, profile, model, fetch=lambda j: JD_PYTHON)
    run = fit.screen_fit(store, profile, model, fetch=lambda j: pytest.fail("refetched"))
    assert len(model.calls) == 1
    assert run.counts == {}


def test_reopened_job_with_recorded_mismatch_is_skipped_again(store, profile):
    [job] = pending(store)
    fit.screen_fit(store, profile, FakeLLM(answer("mismatch")), fetch=lambda j: JD_PYTHON)
    store.set_status(job.id, Status.PENDING)  # e.g. re-screened after a location change
    fit.screen_fit(store, profile, FakeLLM(), fetch=lambda j: pytest.fail("refetched"))
    assert store.get(job.id).status is Status.SKIPPED


def test_quota_exhausted_stops_and_leaves_jobs_pending(store, profile):
    profile.llm.fit_batch_size = 1
    jobs = pending(store, 3)
    model = FakeLLM(answer("ok"), llm.QuotaExhausted("daily LLM budget used"))
    run = fit.screen_fit(store, profile, model, fetch=lambda j: JD_PYTHON)
    assert run.stopped == "daily LLM budget used"
    assert run.counts == {"checked": 1}
    assert [store.get(j.id).status for j in jobs] == [Status.PENDING] * 3
    assert store.get_fit(jobs[1].id) is None and store.get_fit(jobs[2].id) is None
    # The fetched description is kept, so the next run doesn't refetch it
    assert store.get_description(jobs[1].id) == JD_PYTHON


def test_closed_posting_is_skipped(store, profile):
    [job] = pending(store)

    def fetch(j):
        raise jd_fetch.PostingClosed("job description not found (404)")

    run = fit.screen_fit(store, profile, FakeLLM(), fetch=fetch)
    assert run.counts == {"closed": 1}
    assert store.get(job.id).status is Status.SKIPPED
    assert "posting closed" in store.get(job.id).reason


def test_transient_fetch_error_is_retried_next_run(store, profile):
    [job] = pending(store)

    def fetch(j):
        raise jd_fetch.FetchError("HTTP 503")

    run = fit.screen_fit(store, profile, FakeLLM(), fetch=fetch)
    assert run.counts == {"errors": 1}
    assert store.get(job.id).status is Status.PENDING
    assert store.get_fit(job.id) is None


def test_unsupported_description_passes_unchecked(store, profile):
    [job] = pending(store)

    def fetch(j):
        raise jd_fetch.Unsupported("no public description endpoint")

    run = fit.screen_fit(store, profile, FakeLLM(), fetch=fetch)
    assert run.counts == {"unchecked": 1}
    assert store.get(job.id).status is Status.PENDING  # volume over fit
    assert store.get_fit(job.id).source == "none"
    fit.screen_fit(store, profile, FakeLLM(), fetch=lambda j: pytest.fail("retried"))


def test_limit_caps_new_checks(store, profile):
    pending(store, 3)
    model = FakeLLM(answer())
    run = fit.screen_fit(store, profile, model, fetch=lambda j: JD_PYTHON, limit=2)
    assert run.counts == {"checked": 2}
    assert len(fit.job_ids_in(model.calls[0]["prompt"])) == 2


def test_bad_llm_output_skips_that_job_and_continues(store, profile):
    profile.llm.fit_batch_size = 1
    jobs = pending(store, 2)
    model = FakeLLM(llm.LLMError("unusable output"), answer("ok"))
    run = fit.screen_fit(store, profile, model, fetch=lambda j: JD_PYTHON)
    assert run.counts == {"errors": 1, "checked": 1}
    assert store.get_fit(jobs[0].id) is None  # retried next run
    assert store.get_fit(jobs[1].id).verdict == "ok"


def test_jobs_are_checked_in_batches(store, profile):
    profile.llm.fit_batch_size = 8
    jobs = pending(store, 10)
    model = FakeLLM(answer(), answer())
    run = fit.screen_fit(store, profile, model, fetch=lambda j: JD_PYTHON)
    assert [len(fit.job_ids_in(c["prompt"])) for c in model.calls] == [8, 2]
    assert run.counts == {"checked": 10}
    assert all(store.get_fit(j.id).verdict == "ok" for j in jobs)


def test_job_missing_from_batch_answer_is_retried_next_run(store, profile):
    jobs = pending(store, 2)

    class Partial:
        def generate(self, **kw):
            first = fit.job_ids_in(kw["prompt"])[0]
            return fit.FitBatch(results=[fit.FitItem(id=first, **answer().model_dump())])

    run = fit.screen_fit(store, profile, Partial(), fetch=lambda j: JD_PYTHON)
    assert run.counts == {"checked": 1, "errors": 1}
    assert store.get_fit(jobs[1].id) is None


def test_rubric_treats_citizenship_and_clearance_as_mismatch():
    assert "citizenship" in fit.SYSTEM and "clearance" in fit.SYSTEM
    assert "mismatch" in fit.SYSTEM.split("citizenship")[0].rsplit("\n- ", 1)[-1]
