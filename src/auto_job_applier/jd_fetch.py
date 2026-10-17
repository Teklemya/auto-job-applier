"""Job description text from each ATS's public JSON endpoint (no browser).

- Greenhouse: boards API, `content` is HTML-escaped HTML
- Lever: postings API, plain-text description + HTML bullet lists
- Workday: the `cxs` JSON that the career site itself calls
"""

import html
import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

from .models import ATS, Job

_BLOCK_TAGS = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr"}
_LOCALE = re.compile(r"^[a-z]{2}-[A-Z]{2}$")


class Unsupported(Exception):
    """No public description for this URL. Not retried; the job passes unchecked."""


class PostingClosed(Exception):
    """The ATS says the posting is gone (404/410)."""


class FetchError(Exception):
    """Network or server trouble. Worth retrying on the next run."""


def api_url(job: Job) -> str:
    parts = urlsplit(job.url)
    host = (parts.hostname or "").lower()
    segs = [s for s in parts.path.split("/") if s]
    if job.ats is ATS.GREENHOUSE:
        if segs[:1] == ["embed"]:
            q = parse_qs(parts.query)
            if "for" in q and "token" in q:
                return (
                    f"https://boards-api.greenhouse.io/v1/boards/{q['for'][0]}/jobs/{q['token'][0]}"
                )
        elif len(segs) >= 3 and segs[1] == "jobs":
            return f"https://boards-api.greenhouse.io/v1/boards/{segs[0]}/jobs/{segs[2]}"
    elif job.ats is ATS.LEVER and len(segs) >= 2:
        api_host = host.replace("jobs.", "api.", 1)
        return f"https://{api_host}/v0/postings/{segs[0]}/{segs[1]}"
    elif job.ats is ATS.WORKDAY:
        if segs and _LOCALE.match(segs[0]):
            segs = segs[1:]
        if host.endswith("myworkdaysite.com") and segs[:1] == ["recruiting"]:
            tenant, segs = (segs[1] if len(segs) > 1 else ""), segs[2:]
        else:
            tenant = host.split(".")[0]
        if tenant and len(segs) >= 3 and segs[1] == "job":
            return f"https://{host}/wday/cxs/{tenant}/{segs[0]}/{'/'.join(segs[1:])}"
    raise Unsupported(f"no public description endpoint for {job.url}")


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.chunks: list[str] = []

    def handle_starttag(self, tag, attrs):
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in _BLOCK_TAGS:
            self.chunks.append("\n")

    def handle_data(self, data):
        self.chunks.append(data)


def html_to_text(markup: str) -> str:
    parser = _Text()
    parser.feed(markup)
    lines = (
        " ".join(line.replace("\xa0", " ").split()) for line in "".join(parser.chunks).split("\n")
    )
    return "\n".join(line for line in lines if line)


def parse_description(ats: ATS, payload: dict) -> str:
    if ats is ATS.GREENHOUSE:
        return html_to_text(html.unescape(payload.get("content") or ""))
    if ats is ATS.LEVER:
        parts = [payload.get("descriptionPlain") or ""]
        for item in payload.get("lists") or []:
            parts.append(f"{item.get('text', '')}\n{html_to_text(item.get('content') or '')}")
        parts.append(payload.get("additionalPlain") or "")
        return "\n\n".join(p.strip() for p in parts if p.strip())
    if ats is ATS.WORKDAY:
        info = payload.get("jobPostingInfo") or {}
        return html_to_text(info.get("jobDescription") or "")
    raise Unsupported(f"no description parser for {ats}")


def _get_json(url: str, timeout: float = 30) -> dict:
    req = urllib.request.Request(
        url, headers={"User-Agent": "auto-job-applier", "Accept": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_description(job: Job, get: Callable[[str], dict] = _get_json) -> str:
    url = api_url(job)
    try:
        payload = get(url)
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):
            raise PostingClosed(f"job description not found ({e.code})") from e
        raise FetchError(f"{url}: HTTP {e.code}") from e
    except (OSError, ValueError) as e:  # URLError, timeouts, bad JSON
        raise FetchError(f"{url}: {e}") from e
    text = parse_description(job.ats, payload)
    if not text:
        raise Unsupported(f"empty description at {url}")
    return text
