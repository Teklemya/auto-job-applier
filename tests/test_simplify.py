from conftest import FIXTURES

from auto_job_applier.models import ATS
from auto_job_applier.sources.ats_detect import canonical_url, detect_ats
from auto_job_applier.sources.simplify import parse_simplify


def load_fixture():
    return parse_simplify((FIXTURES / "simplify_readme.md").read_text())


def row(company, role, location, href):
    apply = f'<a href="{href}"><img src="x.png" alt="Apply"></a>' if href else "🔒"
    return f"<tr>\n<td>{company}</td>\n<td>{role}</td>\n<td>{location}</td>\n<td>{apply}</td>\n<td>2d</td>\n</tr>"  # noqa: E501


def wrap(*rows, heading="## 💻 Software Engineering New Grad Roles"):
    body = "\n".join(rows)
    return f"{heading}\n\n<table>\n<thead><tr><th>Company</th></tr></thead>\n<tbody>\n{body}\n</tbody>\n</table>\n"  # noqa: E501


# --- fixture: real README structure -------------------------------------------------------


def test_only_open_swe_rows_are_parsed():
    jobs = load_fixture()
    # 14 active SWE rows; the 2 🔒 inactive rows and the Product Management row are excluded
    assert len(jobs) == 14
    assert not any("Product Management" in j.role for j in jobs)
    assert {j.company for j in jobs}.isdisjoint({"Qualtrics", "Arcfield", "Applied Materials"})


def test_continuation_rows_inherit_the_company():
    jobs = load_fixture()
    dexcom = [j for j in jobs if j.company == "Dexcom"]
    assert [j.location for j in dexcom] == ["California", "Oregon"]
    assert not any(j.company == "↳" for j in jobs)


def test_multi_location_cells_are_flattened():
    axon = next(j for j in load_fixture() if j.company == "Axon")
    assert axon.location == "Boston, MA; Seattle, WA; Scottsdale, AZ; Atlanta, GA"


def test_flags_are_extracted_and_stripped_from_text():
    jobs = load_fixture()
    nvidia = next(j for j in jobs if j.company == "NVIDIA")
    assert "faang" in nvidia.flags
    grad = next(j for j in jobs if "Master" in j.role)
    assert "advanced_degree" in grad.flags
    assert "🎓" not in grad.role


def test_ats_detected_and_urls_canonical():
    jobs = {j.company: j for j in load_fixture()}
    assert jobs["Axon"].ats is ATS.GREENHOUSE
    assert jobs["Dexcom"].ats is ATS.WORKDAY
    assert jobs["Shield AI"].ats is ATS.LEVER
    assert jobs["Aurelian"].ats is ATS.MANUAL  # Ashby: not supported in v1
    assert jobs["Planview"].ats is ATS.MANUAL
    assert jobs["Axon"].url == "https://job-boards.greenhouse.io/axon/jobs/7703668003"
    assert all("utm_source" not in j.url and "ref=" not in j.url for j in jobs.values())
    assert all(j.source == "simplify" and j.age for j in jobs.values())


# --- inline rows: flags absent from today's active list -----------------------------------


def test_sponsorship_and_citizenship_flags():
    md = wrap(
        row(
            "<strong>Acme</strong>",
            "Full-Stack Engineer 🛂",
            "Remote in USA",
            "https://jobs.lever.co/acme/1",
        ),
        row(
            "Beta",
            "Software Engineer I 🇺🇸",
            "Arlington, VA",
            "https://beta.wd1.myworkdayjobs.com/x/job/1",
        ),
    )
    acme, beta = parse_simplify(md)
    assert acme.flags == ["no_sponsorship"] and acme.role == "Full-Stack Engineer"
    assert beta.flags == ["us_citizenship"]


def test_closed_rows_are_dropped():
    md = wrap(row("Acme", "SWE", "Remote", None), row("Beta", "SWE", "Remote", "https://x.io/1"))
    assert [j.company for j in parse_simplify(md)] == ["Beta"]


def test_other_sections_are_ignored():
    md = wrap(row("Acme", "Data Scientist", "NYC", "https://x.io/1"), heading="## 🤖 Data Science")
    assert parse_simplify(md) == []


# --- ATS detection / canonicalization -----------------------------------------------------


def test_detect_ats_by_domain():
    assert detect_ats("https://acme.wd5.myworkdayjobs.com/en-US/External/job/X") is ATS.WORKDAY
    assert detect_ats("https://wd3.myworkdaysite.com/recruiting/acme/x") is ATS.WORKDAY
    assert detect_ats("https://boards.greenhouse.io/acme/jobs/1") is ATS.GREENHOUSE
    assert detect_ats("https://job-boards.eu.greenhouse.io/acme/jobs/1") is ATS.GREENHOUSE
    assert detect_ats("https://jobs.eu.lever.co/acme/123") is ATS.LEVER
    assert detect_ats("https://greenhouse.io.evil.com/x") is ATS.MANUAL
    assert detect_ats("https://www.acme.com/careers?gh_jid=1") is ATS.MANUAL


def test_canonical_url_strips_tracking_and_lever_apply_suffix():
    assert (
        canonical_url("https://Jobs.Lever.co/acme/123/apply?utm_source=Simplify&ref=Simplify")
        == "https://jobs.lever.co/acme/123"
    )
    assert canonical_url("https://x.io/job?id=7&utm_medium=company#frag") == "https://x.io/job?id=7"
