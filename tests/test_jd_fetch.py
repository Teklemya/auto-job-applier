import json
import urllib.error

import pytest
from conftest import FIXTURES

from auto_job_applier import jd_fetch
from auto_job_applier.models import ATS, Job
from auto_job_applier.sources.ats_detect import detect_ats


def _job(url: str) -> Job:
    return Job(url=url, company="Acme", role="Software Engineer", ats=detect_ats(url))


@pytest.mark.parametrize(
    ("url", "api"),
    [
        (
            "https://job-boards.greenhouse.io/affirm/jobs/8017340003",
            "https://boards-api.greenhouse.io/v1/boards/affirm/jobs/8017340003",
        ),
        (
            "https://boards.greenhouse.io/axon/jobs/7703668003",
            "https://boards-api.greenhouse.io/v1/boards/axon/jobs/7703668003",
        ),
        (
            "https://boards.greenhouse.io/embed/job_app?for=affirm&token=8017340003",
            "https://boards-api.greenhouse.io/v1/boards/affirm/jobs/8017340003",
        ),
        (
            "https://jobs.lever.co/shieldai/59e0cf9b-04d7-43c0-8a65-9d411fe56c68",
            "https://api.lever.co/v0/postings/shieldai/59e0cf9b-04d7-43c0-8a65-9d411fe56c68",
        ),
        (
            "https://jobs.eu.lever.co/acme/59e0cf9b-04d7-43c0-8a65-9d411fe56c68",
            "https://api.eu.lever.co/v0/postings/acme/59e0cf9b-04d7-43c0-8a65-9d411fe56c68",
        ),
        (
            "https://dexcom.wd1.myworkdayjobs.com/Dexcom/job/Remote-California/SW-Development-Engineer-1_JR121398",
            "https://dexcom.wd1.myworkdayjobs.com/wday/cxs/dexcom/Dexcom/job/Remote-California/SW-Development-Engineer-1_JR121398",
        ),
        (
            "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/US-CA-Santa-Clara/SoC_JR2026744",
            "https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/NVIDIAExternalCareerSite/job/US-CA-Santa-Clara/SoC_JR2026744",
        ),
        (
            "https://wd5.myworkdaysite.com/recruiting/acme/Careers/job/Austin-TX/SWE_R1",
            "https://wd5.myworkdaysite.com/wday/cxs/acme/Careers/job/Austin-TX/SWE_R1",
        ),
    ],
)
def test_api_url(url, api):
    assert jd_fetch.api_url(_job(url)) == api


@pytest.mark.parametrize(
    "url",
    [
        "https://careers.example.com/jobs/123",  # MANUAL site
        "https://job-boards.greenhouse.io/affirm",  # board, not a job
        "https://acme.wd1.myworkdayjobs.com/External/details/SWE_R1",  # unknown Workday shape
    ],
)
def test_api_url_unsupported(url):
    with pytest.raises(jd_fetch.Unsupported):
        jd_fetch.api_url(_job(url))


@pytest.mark.parametrize(
    ("ats", "fixture", "expect"),
    [
        (ATS.GREENHOUSE, "greenhouse.json", "At Affirm, we exist"),
        (ATS.LEVER, "lever.json", "Required qualifications:"),
        (ATS.WORKDAY, "workday.json", "Who are we looking for?"),
    ],
)
def test_parse_description_is_plain_text(ats, fixture, expect):
    payload = json.loads((FIXTURES / "jd" / fixture).read_text())
    text = jd_fetch.parse_description(ats, payload)
    assert expect in text
    assert "<" not in text and "&lt;" not in text and "&nbsp;" not in text
    assert len(text) > 1000


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://x", code, "err", {}, None)


def test_fetch_uses_api_and_parses():
    payload = json.loads((FIXTURES / "jd" / "lever.json").read_text())
    seen = []

    def get(url):
        seen.append(url)
        return payload

    job = _job("https://jobs.lever.co/shieldai/59e0cf9b-04d7-43c0-8a65-9d411fe56c68")
    text = jd_fetch.fetch_description(job, get=get)
    assert seen == [
        "https://api.lever.co/v0/postings/shieldai/59e0cf9b-04d7-43c0-8a65-9d411fe56c68"
    ]
    assert "Shield AI" in text


@pytest.mark.parametrize("code", [404, 410])
def test_fetch_missing_posting_is_closed(code):
    def get(url):
        raise _http_error(code)

    with pytest.raises(jd_fetch.PostingClosed):
        jd_fetch.fetch_description(_job("https://job-boards.greenhouse.io/a/jobs/1"), get=get)


@pytest.mark.parametrize("err", [_http_error(500), urllib.error.URLError("down"), TimeoutError()])
def test_fetch_transient_errors_propagate_as_fetch_error(err):
    def get(url):
        raise err

    with pytest.raises(jd_fetch.FetchError):
        jd_fetch.fetch_description(_job("https://job-boards.greenhouse.io/a/jobs/1"), get=get)


def test_fetch_empty_description_is_unsupported():
    with pytest.raises(jd_fetch.Unsupported):
        jd_fetch.fetch_description(
            _job("https://job-boards.greenhouse.io/a/jobs/1"), get=lambda url: {"content": ""}
        )
