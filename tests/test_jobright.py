import copy
import json

from conftest import FIXTURES
from typer.testing import CliRunner

from auto_job_applier.cli import app
from auto_job_applier.models import ATS
from auto_job_applier.sources import jobright

runner = CliRunner()


def feed():
    """A real recommended-jobs response, trimmed to the fields the reader uses."""
    return json.loads((FIXTURES / "jobright_recommend.json").read_text())


def entry(payload, company):
    return next(
        x for x in payload["result"]["jobList"] if x["companyResult"]["companyName"] == company
    )


def by_company(jobs, company):
    return next(j for j in jobs if j.company == company)


# --- parsing the real feed -------------------------------------------------------------------


def test_every_job_above_the_match_threshold_is_read():
    jobs = jobright.parse_jobs(feed())
    assert len(jobs) == 20  # the sample's lowest match is 84.7%
    assert all(j.source == "jobright" for j in jobs)


def test_job_points_at_the_company_application_page_not_jobright():
    visa = by_company(jobright.parse_jobs(feed()), "Visa")
    assert visa.url.startswith("https://visa.wd5.myworkdayjobs.com/visa/job/")
    assert visa.ats is ATS.WORKDAY
    assert visa.role == "SW Engineer"
    assert visa.location == "Denver, CO"
    assert visa.age == "49 minutes ago"


def test_greenhouse_embed_links_are_detected_as_greenhouse():
    lyft = by_company(jobright.parse_jobs(feed()), "Lyft")
    assert lyft.ats is ATS.GREENHOUSE


def test_unsupported_sites_are_listed_for_manual_apply():
    jobs = jobright.parse_jobs(feed())
    assert by_company(jobs, "Altimetrik").ats is ATS.MANUAL  # linkedin.com
    assert by_company(jobs, "KAYAK").ats is ATS.MANUAL  # ashbyhq.com


def test_multiple_locations_are_joined_like_simplify():
    intuit = by_company(jobright.parse_jobs(feed()), "Intuit")
    assert intuit.location.startswith("Mountain View, CA; San Diego, CA; New York, NY")


def test_remote_jobs_are_marked_remote_for_location_screening():
    zelis = by_company(jobright.parse_jobs(feed()), "Zelis")
    assert zelis.location == "Remote - United States"


def test_citizen_only_jobs_carry_the_citizenship_flag():
    mongodb = by_company(jobright.parse_jobs(feed()), "MongoDB")
    assert mongodb.flags == ["us_citizenship"]


def test_clearance_jobs_carry_the_citizenship_flag():
    payload = feed()
    entry(payload, "Visa")["jobResult"]["isClearanceRequired"] = True
    visa = by_company(jobright.parse_jobs(payload), "Visa")
    assert visa.flags == ["us_citizenship"]


# --- which jobs count --------------------------------------------------------------------------


def test_jobs_at_or_below_80_percent_are_ignored():
    payload = feed()
    entry(payload, "Visa")["displayScore"] = 80.0
    jobs = jobright.parse_jobs(payload)
    assert "Visa" not in {j.company for j in jobs}


def test_liked_jobs_are_read_whatever_their_match():
    payload = feed()
    visa = entry(payload, "Visa")
    visa["displayScore"] = 40.0
    visa["isLiked"] = True
    assert "Visa" in {j.company for j in jobright.parse_jobs(payload)}


def test_deleted_postings_are_ignored():
    payload = feed()
    entry(payload, "Visa")["jobResult"]["isDeleted"] = True
    assert "Visa" not in {j.company for j in jobright.parse_jobs(payload)}


def test_a_job_without_any_link_is_ignored():
    payload = feed()
    job = entry(payload, "Visa")["jobResult"]
    job["originalUrl"] = job["applyLink"] = ""
    assert "Visa" not in {j.company for j in jobright.parse_jobs(payload)}


def test_responses_that_are_not_job_lists_are_ignored():
    assert jobright.parse_jobs({"success": True, "result": {"balance": 3}}) == []
    assert jobright.parse_jobs({"success": False, "errorCode": 401, "result": None}) == []


def test_a_job_seen_in_two_responses_is_read_once():
    jobs = jobright.parse_responses([feed(), copy.deepcopy(feed())])
    assert len(jobs) == 20


# --- aja collect ---------------------------------------------------------------------------


def test_collect_jobright_queues_and_screens_the_feed(aja_home, monkeypatch):
    monkeypatch.setattr(jobright, "fetch_responses", lambda: [feed()])
    result = runner.invoke(app, ["collect", "--source", "jobright"])
    assert result.exit_code == 0, result.output
    assert "Jobright: 20 roles" in result.output
    listed = runner.invoke(app, ["list", "--status", "SKIPPED"])
    assert "MongoDB" in listed.output  # citizenship flag → hard filter


def test_collect_jobright_never_retires_jobs_missing_from_the_feed(aja_home, monkeypatch):
    # The feed is a rolling recommendation list; a job dropping out of it isn't closed.
    monkeypatch.setattr(jobright, "fetch_responses", lambda: [feed()])
    runner.invoke(app, ["collect", "--source", "jobright"])
    smaller = feed()
    smaller["result"]["jobList"] = smaller["result"]["jobList"][:1]
    monkeypatch.setattr(jobright, "fetch_responses", lambda: [smaller])
    result = runner.invoke(app, ["collect", "--source", "jobright"])
    assert "Retired" not in result.output


def test_collect_jobright_says_to_log_in_when_no_feed_loads(aja_home, monkeypatch):
    def signed_out():
        raise jobright.JobrightError("No job list loaded. Run `aja login`.")

    monkeypatch.setattr(jobright, "fetch_responses", signed_out)
    result = runner.invoke(app, ["collect", "--source", "jobright"])
    assert result.exit_code == 1
    assert "aja login" in result.output


def test_collect_jobright_fails_loudly_when_nothing_parses(aja_home, monkeypatch):
    monkeypatch.setattr(jobright, "fetch_responses", lambda: [{"success": True, "result": {}}])
    result = runner.invoke(app, ["collect", "--source", "jobright"])
    assert result.exit_code == 1
    assert "format may have changed" in result.output


# --- the Liked page ---------------------------------------------------------------------------


def liked():
    """A real Liked-page response: same job items, wrapped in result.list instead of jobList."""
    return json.loads((FIXTURES / "jobright_liked.json").read_text())


def test_liked_page_jobs_are_read():
    jobs = jobright.parse_jobs(liked())
    assert {j.company for j in jobs} == {"Intuit", "Lyft", "Visa"}


def test_liked_page_jobs_are_read_whatever_their_match():
    payload = liked()
    for item in payload["result"]["list"]:
        item["displayScore"] = 10.0
    assert len(jobright.parse_jobs(payload)) == 3


def test_liked_page_counts_as_a_job_list():
    assert jobright.has_job_list([liked()])


def test_other_lists_that_hold_no_jobs_are_ignored():
    notifications = {"success": True, "result": {"list": [{"msgId": 1, "text": "hi"}]}}
    assert not jobright.has_job_list([notifications])
    assert jobright.parse_jobs(notifications) == []


def test_a_job_both_liked_and_recommended_is_read_once():
    jobs = jobright.parse_responses([feed(), liked()])
    assert len(jobs) == 20  # all three liked jobs are also in the feed sample


def test_more_pages_follow_while_the_liked_list_says_has_more():
    payload = liked()
    assert not jobright.has_more(payload)
    payload["result"]["hasMore"] = True
    assert jobright.has_more(payload)
