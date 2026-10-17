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
