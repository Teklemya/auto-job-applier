import pytest

from auto_job_applier.models import ATS, Job, Status
from auto_job_applier.profile import Targeting
from auto_job_applier.screen import Tier, hard_filter, screen_new
from auto_job_applier.store import JobStore

T = Targeting()


def job(role="Software Engineer I", company="Acme", location="Remote in USA", flags=(), **kw):
    kw.setdefault(
        "url", f"https://boards.greenhouse.io/{company.lower()}/jobs/{abs(hash(role + location))}"
    )
    kw.setdefault("ats", ATS.GREENHOUSE)
    kw.setdefault("source", "simplify")
    return Job(company=company, role=role, location=location, flags=list(flags), **kw)


# --- hard skips (R4) ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("flag", "reason"),
    [
        ("no_sponsorship", "no sponsorship"),
        ("us_citizenship", "citizenship required"),
        ("advanced_degree", "advanced degree"),  # owner has a bachelor's only
    ],
)
def test_flag_skips(flag, reason):
    v = hard_filter(job(flags=[flag]), T)
    assert v.status is Status.SKIPPED and reason in v.reason


@pytest.mark.parametrize(
    "role",
    [
        "Senior Software Engineer",
        "Sr. Backend Engineer",
        "Staff Engineer",
        "Principal Software Engineer",
        "Engineering Manager",
        "Software Engineer III",
        "Software Developer 3",
        "Tech Lead, Payments",
    ],
)
def test_senior_titles_are_skipped(role):
    v = hard_filter(job(role=role), T)
    assert v.status is Status.SKIPPED and "senior" in v.reason


@pytest.mark.parametrize(
    "role",
    [
        "Software Engineer 2 New Grad - Developer Portal",  # real title: "2" + New Grad
        "Software Engineer 1 to 3 - Digital Engineering",
        "Entry Level & Senior Software Engineer - Core AI",
        "Software Engineer II",
        "Junior DevOps Engineer",
        "Leadership-minded Software Engineer I",
    ],
)
def test_early_career_titles_are_kept(role):
    assert hard_filter(job(role=role), T).status is Status.PENDING


# --- location priority (R6) ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("location", "company", "flags", "tier"),
    [
        ("Remote in USA", "Acme", [], Tier.REMOTE),
        ("Remote", "Acme", [], Tier.REMOTE),
        ("Olathe, KS", "Garmin", [], Tier.KC_METRO),
        ("Kansas City, MO", "Acme", [], Tier.KC_METRO),
        ("SF", "NVIDIA", ["faang"], Tier.HUB),
        ("SF", "Unknown Startup", [], Tier.HUB),  # any company counts in a hub
        ("NYC", "Acme", [], Tier.HUB),
        ("Austin, TX", "Acme", [], Tier.HUB),
        ("Chicago, IL", "Acme", [], Tier.HUB),
        ("LA", "Acme", [], Tier.HUB),
        ("Alpharetta, GA", "Acme", [], Tier.HUB),  # Atlanta metro
        ("San Mateo, CA", "Acme", [], Tier.HUB),  # Bay Area suburb
        ("Vienna, VA", "Acme", [], Tier.HUB),  # DMV
        ("Seattle, WA; Remote in USA", "Acme", [], Tier.REMOTE),  # best location wins
        ("United States", "Acme", [], Tier.US_UNSPECIFIED),
        ("Remote - US", "Acme", [], Tier.REMOTE),
        ("Remote, California", "Acme", [], Tier.REMOTE),  # US state after "Remote"
        ("Washington, DC", "Acme", [], Tier.HUB),
        ("Portland, OR", "Acme", [], Tier.HUB),
        ("Lawrence, KS", "Acme", [], Tier.KC_METRO),
        ("Olathe", "Garmin", [], Tier.KC_METRO),  # no state given: city name is enough
        ("Greater Seattle Area", "Plaid", [], Tier.HUB),  # LinkedIn-style area (Jobright)
        ("Kansas City Metropolitan Area", "Acme", [], Tier.KC_METRO),
    ],
)
def test_location_tiers(location, company, flags, tier):
    v = hard_filter(job(location=location, company=company, flags=flags), T)
    assert v.status is Status.PENDING and v.tier is tier


@pytest.mark.parametrize(
    ("location", "company", "flags"),
    [
        ("Remote in Canada", "Acme", []),
        ("London, UK", "Acme", ["faang"]),
        ("Cedar Rapids, IA", "RTX", []),  # not a hub
        ("New Orleans, LA", "Acme", []),  # "LA" here is Louisiana, not Los Angeles
        ("Annapolis Junction, MD", "Acme", []),  # clearance-heavy NSA corridor, not DMV list
        ("O'Fallon, MO", "Mastercard", []),  # Missouri, but St. Louis, not KC
        # Review #1: remote roles abroad must not pass as US remote
        ("Remote - Portugal", "Acme", []),
        ("Remote (LATAM)", "Acme", []),
        ("Remote, Philippines", "Acme", []),
        ("Remote - EMEA", "Acme", []),
        # Review #4: same-named cities in other states
        ("Lawrence, MA", "Acme", []),
        ("Independence, OH", "Acme", []),
        ("Portland, ME", "Acme", []),
        ("Cambridge, UK", "Acme", []),
        ("Arlington, TX", "Acme", []),
        ("California", "Acme", []),  # state only, not remote
    ],
)
def test_location_skips(location, company, flags):
    v = hard_filter(job(location=location, company=company, flags=flags), T)
    assert v.status is Status.SKIPPED and "location" in v.reason


def test_remote_hint_in_workday_url():
    # Real Dexcom posting: location "California", URL path ".../job/Remote-California/..."
    j = job(
        location="California",
        url="https://dexcom.wd1.myworkdayjobs.com/Dexcom/job/Remote-California/SW_JR1",
        ats=ATS.WORKDAY,
    )
    assert hard_filter(j, T).tier is Tier.REMOTE


def test_remote_hint_requires_workday_and_us_segment():
    # Review #3: a "remote" slug elsewhere in the URL is not a location
    greenhouse = job(
        location="Lisbon, Portugal", url="https://boards.greenhouse.io/remotecom/jobs/1"
    )
    assert hard_filter(greenhouse, T).status is Status.SKIPPED
    canada = job(
        location="Toronto, ON, Canada",
        url="https://x.wd1.myworkdayjobs.com/X/job/Remote-Canada/SWE_1",
        ats=ATS.WORKDAY,
    )
    assert hard_filter(canada, T).status is Status.SKIPPED
    title_slug = job(
        location="Tucson, AZ",
        url="https://x.wd1.myworkdayjobs.com/X/job/Tucson-AZ/Remote-Sensing-Software-Engineer_1",
        ats=ATS.WORKDAY,
    )
    assert hard_filter(title_slug, T).status is Status.SKIPPED
    # Jobvite also uses /job/<slug>/, but that slug is an ID, not a location
    jobvite = job(
        location="Tucson, AZ", url="https://jobs.jobvite.com/acme/job/Remote-USA", ats=ATS.MANUAL
    )
    assert hard_filter(jobvite, T).status is Status.SKIPPED


def test_unsupported_ats_becomes_manual():
    v = hard_filter(job(ats=ATS.MANUAL), T)
    assert v.status is Status.MANUAL and v.tier is Tier.REMOTE


# --- screen_new: persistence + dedupe (R7, R9) --------------------------------------------


@pytest.fixture
def store(tmp_path):
    s = JobStore(tmp_path / "jobs.db")
    yield s
    s.close()


def test_screen_new_sets_status_reason_and_tier(store):
    store.upsert_many([job(), job(role="Senior Software Engineer")])
    counts = screen_new(store, T)
    assert counts == {Status.PENDING: 1, Status.SKIPPED: 1}
    by_role = {j.role: j for j in store.list()}
    assert by_role["Software Engineer I"].tier is Tier.REMOTE
    assert "senior" in by_role["Senior Software Engineer"].reason
    assert screen_new(store, T) == {}  # only NEW jobs are screened


def test_duplicate_company_and_title_is_skipped(store):
    # Real case: Dexcom posts one role per state; apply once.
    store.upsert_many(
        [
            job(company="Dexcom", role="SW Development Engineer 1", url="https://a.io/ca"),
            job(company="Dexcom ", role="SW Development Engineer 1", url="https://a.io/or"),
        ]
    )
    screen_new(store, T)
    first, second = store.list()
    assert first.status is Status.PENDING
    assert second.status is Status.SKIPPED and f"duplicate of #{first.id}" in second.reason


def test_location_filter_runs_before_dedupe(store):
    # Real case: Affirm lists Remote-in-Canada first, then the same role Remote-in-USA.
    store.upsert_many(
        [
            job(company="Affirm", role="SWE 1", location="Remote in Canada", url="https://a.io/1"),
            job(company="Affirm", role="SWE 1", location="Remote in USA", url="https://a.io/2"),
        ]
    )
    screen_new(store, T)
    canada, usa = store.list()
    assert "location" in canada.reason
    assert usa.status is Status.PENDING


def test_dedupe_against_previously_screened_jobs(store):
    store.upsert_many([job(company="Acme", role="SWE I", url="https://a.io/1")])
    screen_new(store, T)
    store.upsert_many([job(company="ACME", role="swe i", url="https://jobright.ai/x")])
    screen_new(store, T)
    assert [j.status for j in store.list()] == [Status.PENDING, Status.SKIPPED]


@pytest.mark.parametrize("claimed", [Status.STAGED, Status.APPLIED, Status.FAILED, Status.MANUAL])
def test_in_flight_and_finished_jobs_claim_their_role(store, claimed):
    store.upsert_many([job(company="Acme", role="SWE I", url="https://a.io/1", ats=ATS.MANUAL)])
    [first] = store.list()
    store.set_status(first.id, claimed)
    store.upsert_many([job(company="Acme", role="SWE I", url="https://a.io/2", ats=ATS.MANUAL)])
    screen_new(store, T)
    assert store.list()[1].reason == f"duplicate of #{first.id}"


def test_manual_duplicates_are_skipped(store):
    store.upsert_many(
        [
            job(
                company="Garmin",
                role="SWE 1",
                location="Olathe, KS",
                url="https://g.io/1",
                ats=ATS.MANUAL,
            ),
            job(
                company="Garmin",
                role="SWE 1",
                location="Olathe, KS",
                url="https://g.io/2",
                ats=ATS.MANUAL,
            ),
        ]
    )
    screen_new(store, T)
    assert [j.status for j in store.list()] == [Status.MANUAL, Status.SKIPPED]


def test_fillable_posting_takes_the_role_from_a_manual_one(store):
    # Review #5: the auto-applyable copy must win over the unsupported-site copy
    store.upsert_many(
        [
            job(company="Acme", role="SWE I", url="https://careers.acme.com/1", ats=ATS.MANUAL),
            job(company="Acme", role="SWE I", url="https://boards.greenhouse.io/acme/jobs/2"),
        ]
    )
    screen_new(store, T)
    manual, fillable = store.list()
    assert fillable.status is Status.PENDING
    assert manual.status is Status.SKIPPED and manual.reason == f"duplicate of #{fillable.id}"


# --- re-screening (review #2) -------------------------------------------------------------


def test_queued_job_is_rescreened_when_flags_change(store):
    store.upsert_many([job(company="Acme", role="SWE I", url="https://a.io/1")])
    screen_new(store, T)
    store.upsert_many(
        [job(company="Acme", role="SWE I", url="https://a.io/1", flags=["no_sponsorship"])]
    )
    screen_new(store, T)
    [j] = store.list()
    assert j.status is Status.SKIPPED and "no sponsorship" in j.reason


def test_skipped_job_is_rescreened_when_location_changes(store):
    store.upsert_many([job(location="Tucson, AZ", url="https://a.io/1")])
    screen_new(store, T)
    store.upsert_many([job(location="Remote in USA", url="https://a.io/1")])
    screen_new(store, T)
    assert store.list()[0].status is Status.PENDING


def test_in_flight_jobs_are_never_reopened(store):
    store.upsert_many([job(url="https://a.io/1")])
    [j] = store.list()
    store.set_status(j.id, Status.STAGED, "draft")
    store.upsert_many([job(url="https://a.io/1", flags=["no_sponsorship"])])
    assert store.get(j.id).status is Status.STAGED


def test_jobs_no_longer_listed_are_retired(store):
    store.upsert_many([job(url="https://a.io/1"), job(url="https://a.io/2", role="SWE II")])
    screen_new(store, T)
    [kept, gone] = store.list()
    store.set_status(gone.id, Status.PENDING)
    retired = store.retire_missing("simplify", seen_urls={kept.url})
    assert retired == 1
    assert store.get(gone.id).status is Status.SKIPPED
    assert store.get(gone.id).reason == "no longer listed (closed)"
    assert store.get(kept.id).status is Status.PENDING


# --- duplicates are released when their owner leaves (review 2, #1) -----------------------


def _dexcom_pair(store):
    # Real pattern: one Workday req per state for the same role
    ca = job(company="Dexcom", role="SWE 1", url="https://a.io/ca")
    oregon = job(company="Dexcom", role="SWE 1", url="https://a.io/or")
    store.upsert_many([ca, oregon])
    screen_new(store, T)
    return store.list()


def test_duplicate_is_released_when_its_owner_closes(store):
    owner, dup = _dexcom_pair(store)
    store.retire_missing("simplify", seen_urls={dup.url})
    screen_new(store, T)
    assert store.get(owner.id).status is Status.SKIPPED
    assert store.get(dup.id).status is Status.PENDING


def test_duplicate_is_released_when_its_owner_is_rescreened_out(store):
    owner, dup = _dexcom_pair(store)
    store.upsert_many(
        [job(company="Dexcom", role="SWE 1", url=owner.url, flags=["no_sponsorship"])]
    )
    screen_new(store, T)
    assert "no sponsorship" in store.get(owner.id).reason
    assert store.get(dup.id).status is Status.PENDING


def test_duplicate_stays_skipped_while_its_owner_is_active(store):
    owner, dup = _dexcom_pair(store)
    store.set_status(owner.id, Status.STAGED, "draft")
    events_before = len(store.events(dup.id))
    screen_new(store, T)
    assert store.get(dup.id).reason == f"duplicate of #{owner.id}"
    assert len(store.events(dup.id)) == events_before  # not churned through NEW


# --- retire / relist (review 2, #2) -------------------------------------------------------


@pytest.mark.parametrize("status", [Status.NEW, Status.PENDING, Status.MANUAL])
def test_retire_covers_every_not_yet_applied_status(store, status):
    store.upsert_many([job(url="https://a.io/1")])
    [j] = store.list()
    store.set_status(j.id, status)
    assert store.retire_missing("simplify", seen_urls=set()) == 1
    assert store.get(j.id).reason == "no longer listed (closed)"


@pytest.mark.parametrize("status", [Status.STAGED, Status.APPLIED, Status.FAILED])
def test_retire_leaves_in_flight_and_finished_jobs(store, status):
    store.upsert_many([job(url="https://a.io/1")])
    [j] = store.list()
    store.set_status(j.id, status)
    assert store.retire_missing("simplify", seen_urls=set()) == 0


def test_relisted_posting_is_reopened(store):
    store.upsert_many([job(url="https://a.io/1")])
    screen_new(store, T)
    store.retire_missing("simplify", seen_urls=set())
    store.upsert_many([job(url="https://a.io/1")])  # same facts, listed again
    screen_new(store, T)
    assert store.list()[0].status is Status.PENDING
