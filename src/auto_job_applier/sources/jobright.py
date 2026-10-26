"""Jobright reader (R2, D8): Liked jobs plus recommended jobs above an 80% match.

The tool opens the recommended and Liked pages in its own logged-in Chrome profile, like a
human visit, and keeps the job lists those pages load for themselves. It never calls
Jobright's API directly. Each job carries `originalUrl`, the company's own application page,
so no redirect needs following.
"""

from ..models import Job
from .ats_detect import canonical_url, detect_ats

MIN_MATCH = 80.0  # "a match above 80%"
FEED_URL = "https://jobright.ai/jobs/recommend"
LIKED_URL = "https://jobright.ai/jobs/liked"
MAX_FEED_PAGES = 5  # 20 jobs per page; stop earlier once a page has nothing above MIN_MATCH
MAX_LIKED_PAGES = 10


class JobrightError(Exception):
    pass


def _job_list(payload: dict) -> list[dict] | None:
    """The feed wraps jobs in result.jobList, the Liked page in result.list."""
    result = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(result, dict):
        return None
    jobs = result.get("jobList", result.get("list"))
    if not isinstance(jobs, list):
        return None
    if not all(isinstance(item, dict) and "jobResult" in item for item in jobs):
        return None
    return jobs


def has_more(payload: dict) -> bool:
    result = payload.get("result")
    return isinstance(result, dict) and bool(result.get("hasMore"))


def _location(job: dict) -> str:
    places = job.get("jobLocations") or []
    if job.get("isRemote"):
        places = [f"Remote - {p}" for p in places] or ["Remote"]
    return "; ".join(places)


def _to_job(item: dict) -> Job | None:
    job, company = item.get("jobResult") or {}, item.get("companyResult") or {}
    link = job.get("originalUrl") or job.get("applyLink")
    if not link or job.get("isDeleted"):
        return None
    # A security clearance needs US citizenship too, so both map to the citizenship skip.
    citizen_only = job.get("isCitizenOnly") or job.get("isClearanceRequired")
    return Job(
        url=canonical_url(link),
        company=company.get("companyName", ""),
        role=job.get("jobTitle", ""),
        location=_location(job),
        ats=detect_ats(link),
        flags=["us_citizenship"] if citizen_only else [],
        age=job.get("publishTimeDesc", ""),
        source="jobright",
    )


def parse_jobs(payload: dict, min_match: float = MIN_MATCH) -> list[Job]:
    jobs = []
    for item in _job_list(payload) or []:
        if not item.get("isLiked") and (item.get("displayScore") or 0) <= min_match:
            continue
        if job := _to_job(item):
            jobs.append(job)
    return jobs


def parse_responses(payloads: list[dict]) -> list[Job]:
    """Jobs from every job-list response, first sighting wins."""
    seen: dict[str, Job] = {}
    for payload in payloads:
        for job in parse_jobs(payload):
            seen.setdefault(job.url, job)
    return list(seen.values())


def has_job_list(payloads: list[dict]) -> bool:
    return any(_job_list(p) is not None for p in payloads)


def _wait_for(page, done, seconds: float) -> bool:
    for _ in range(int(seconds * 2)):
        if done():
            return True
        page.wait_for_timeout(500)
    return done()


def _scroll_for_more(page, payloads: list[dict]) -> bool:
    """Scroll the list; True once the page has loaded another job list."""
    loaded = len(payloads)
    page.mouse.move(500, 500)
    page.mouse.wheel(0, 20000)
    return _wait_for(page, lambda: len(payloads) > loaded, 10)


def fetch_responses() -> list[dict]:
    """Visit the recommended and Liked pages; return the job lists they loaded."""
    from playwright.sync_api import sync_playwright

    from .. import browser, paths

    payloads: list[dict] = []

    def keep(response) -> None:
        if "jobright.ai/swan/" not in response.url:
            return
        if "json" not in (response.headers.get("content-type") or ""):
            return
        try:
            body = response.json()
        except Exception:  # bodies of redirects and aborted requests can't be read
            return
        if _job_list(body) is not None:
            payloads.append(body)

    with sync_playwright() as p:
        context = browser.open_context(p, paths.browser_profile_dir())
        try:
            page = context.new_page()
            page.on("response", keep)
            page.goto(FEED_URL, wait_until="domcontentloaded")
            if not _wait_for(page, lambda: payloads, 20):
                raise JobrightError(
                    f"Jobright loaded no job list (ended on {page.url}). "
                    "If you were signed out, run `aja login`."
                )
            for _ in range(MAX_FEED_PAGES - 1):
                if not parse_jobs(payloads[-1]) or not _scroll_for_more(page, payloads):
                    break
            feed_pages = len(payloads)
            page.goto(LIKED_URL, wait_until="domcontentloaded")
            if _wait_for(page, lambda: len(payloads) > feed_pages, 15):
                for _ in range(MAX_LIKED_PAGES - 1):
                    if not has_more(payloads[-1]) or not _scroll_for_more(page, payloads):
                        break
        finally:
            context.close()
    return payloads
