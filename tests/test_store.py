import pytest

from auto_job_applier.models import ATS, Job, Status
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
