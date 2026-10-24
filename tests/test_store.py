import pytest

from auto_job_applier.models import ATS, Fit, Job, Status, Tier
from auto_job_applier.store import JobStore


@pytest.fixture
def store(tmp_path):
    s = JobStore(tmp_path / "jobs.db")
    yield s
    s.close()


def make_job(**overrides):
    fields = dict(
        url="https://job-boards.greenhouse.io/acme/jobs/1",
        company="Acme",
        role="Software Engineer I",
        location="Remote",
        ats=ATS.GREENHOUSE,
        flags=["faang"],
        source="simplify",
    )
    fields.update(overrides)
    return Job(**fields)


def test_upsert_round_trips_a_job(store):
    inserted, updated = store.upsert_many([make_job()])
    assert (inserted, updated) == (1, 0)

    [job] = store.list()
    assert job.id is not None
    assert job.company == "Acme"
    assert job.ats is ATS.GREENHOUSE
    assert job.status is Status.NEW
    assert job.flags == ["faang"]
    assert job.created_at is not None


def test_reingest_dedupes_and_preserves_status(store):
    store.upsert_many([make_job()])
    [job] = store.list()
    store.set_status(job.id, Status.APPLIED, "submitted")

    inserted, updated = store.upsert_many([make_job(role="Software Engineer I (Backend)")])

    assert (inserted, updated) == (0, 1)
    [job] = store.list()
    assert job.status is Status.APPLIED  # ingest never resets status
    assert job.role == "Software Engineer I (Backend)"  # metadata refreshed


def test_every_status_transition_writes_an_event(store):
    store.upsert_many([make_job()])
    [job] = store.list()

    store.set_status(job.id, Status.PENDING)
    store.set_status(job.id, Status.FAILED, "selector not found")

    events = store.events(job.id)
    assert [(e["from_status"], e["to_status"]) for e in events] == [
        ("NEW", "PENDING"),
        ("PENDING", "FAILED"),
    ]
    assert events[-1]["message"] == "selector not found"
    assert store.get(job.id).reason == "selector not found"


def test_set_status_on_unknown_job_raises(store):
    with pytest.raises(KeyError):
        store.set_status(999, Status.PENDING)


def test_list_filters_by_status_and_ats(store):
    store.upsert_many(
        [
            make_job(url="https://a.example/1", ats=ATS.WORKDAY),
            make_job(url="https://a.example/2", ats=ATS.LEVER),
        ]
    )
    workday = store.list(ats=ATS.WORKDAY)
    assert [j.url for j in workday] == ["https://a.example/1"]
    assert store.list(status=Status.APPLIED) == []


def test_counts_groups_by_ats_and_status(store):
    store.upsert_many(
        [
            make_job(url="https://a.example/1", ats=ATS.WORKDAY),
            make_job(url="https://a.example/2", ats=ATS.WORKDAY),
            make_job(url="https://a.example/3", ats=ATS.MANUAL),
        ]
    )
    assert store.counts() == {("workday", "NEW"): 2, ("manual", "NEW"): 1}


def test_set_status_without_tier_keeps_the_tier(store):
    from auto_job_applier.models import Tier

    store.upsert_many([make_job()])
    [job] = store.list()
    store.set_status(job.id, Status.PENDING, tier=Tier.HUB)
    store.set_status(job.id, Status.STAGED, "draft")
    assert store.get(job.id).tier is Tier.HUB


def test_list_limit_zero_returns_nothing(store):
    store.upsert_many([make_job()])
    assert store.list(limit=0) == []


# --- answer packets (D3) ---


def _packet():
    from auto_job_applier.engines.base import Packet, PageField
    from auto_job_applier.models import FormField, ResolvedAnswer

    gender = FormField("Gender", "select", required=True, options=["Female", "Male"])
    why = FormField("Why Acme?", "textarea", required=True)
    return Packet(
        fields=[PageField("gender", gender), PageField("question_7", why)],
        answers=[
            ResolvedAnswer(gender, "Female", "profile", confident=True),
            ResolvedAnswer(why, None, "none", confident=False, reason="needs a written answer"),
        ],
        blockers=["Why Acme?"],
    )


def test_packet_round_trips(store):
    store.upsert_many([make_job()])
    job_id = store.list()[0].id
    store.save_packet(job_id, _packet())
    assert store.get_packet(job_id) == _packet()


def test_saving_a_packet_again_replaces_it(store):
    store.upsert_many([make_job()])
    job_id = store.list()[0].id
    store.save_packet(job_id, _packet())
    updated = _packet()
    updated.blockers = []
    store.save_packet(job_id, updated)
    assert store.get_packet(job_id).blockers == []


def test_no_packet_for_a_job_never_filled(store):
    store.upsert_many([make_job()])
    assert store.get_packet(store.list()[0].id) is None


def _pending(store, n, tier=None, score=None, ats=ATS.GREENHOUSE):
    store.upsert_many([make_job(url=f"https://job-boards.greenhouse.io/acme/jobs/{n}", ats=ats)])
    job = store.list()[-1]
    store.set_status(job.id, Status.PENDING, tier=tier)
    if score is not None:
        store.save_fit(job.id, Fit("ok", score, [], "fine", "llm"))
    return job.id


def test_apply_queue_orders_by_tier_then_fit_score(store):
    hub_high = _pending(store, 1, Tier.HUB, 90)
    remote_low = _pending(store, 2, Tier.REMOTE, 40)
    remote_high = _pending(store, 3, Tier.REMOTE, 80)
    _pending(store, 4, Tier.REMOTE)  # not fit-checked yet: waits for a later run
    _pending(store, 5, Tier.REMOTE, 99, ats=ATS.LEVER)
    queue = [j.id for j in store.apply_queue(ATS.GREENHOUSE)]
    assert queue == [remote_high, remote_low, hub_high]
    assert [j.id for j in store.apply_queue(ATS.GREENHOUSE, limit=1)] == [remote_high]


def test_count_events_counts_messages_by_prefix_since_a_time(store):
    job_id = _pending(store, 1)
    store.set_status(job_id, Status.APPLIED, "submit clicked: confirmation page seen")
    store.set_status(job_id, Status.FAILED, "submit clicked: no confirmation page")
    store.set_status(job_id, Status.APPLIED, "submitted by the owner at review")
    assert store.count_events("submit clicked: ", "2000-01-01") == 2
    assert store.count_events("submit clicked: ", "2999-01-01") == 0


def test_new_since_counts_screened_jobs_by_ats(store):
    _pending(store, 1)
    skipped = _pending(store, 2, ats=ATS.WORKDAY)
    store.set_status(skipped, Status.SKIPPED, "senior")
    _pending(store, 3, ats=ATS.LEVER)
    assert store.new_since("2000-01-01") == {"greenhouse": 1, "lever": 1}
    assert store.new_since("2999-01-01") == {}
