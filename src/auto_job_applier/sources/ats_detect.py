"""URL → ATS classification and canonicalization (R3)."""

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..models import ATS

_HOST_SUFFIXES = [
    ("myworkdayjobs.com", ATS.WORKDAY),
    ("myworkdaysite.com", ATS.WORKDAY),
    ("greenhouse.io", ATS.GREENHOUSE),  # boards., job-boards., job-boards.eu.
    ("lever.co", ATS.LEVER),  # jobs.lever.co, jobs.eu.lever.co
]

_TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term", "ref"}


def detect_ats(url: str) -> ATS:
    host = (urlsplit(url).hostname or "").lower()
    for suffix, ats in _HOST_SUFFIXES:
        if host == suffix or host.endswith("." + suffix):
            return ats
    return ATS.MANUAL


def canonical_url(url: str) -> str:
    """Stable identity for dedup: lowercase host, no tracking params, no fragment.

    Lever's `/apply` suffix is dropped so the posting and its apply page are the same job.
    """
    parts = urlsplit(url.strip())
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k not in _TRACKING_PARAMS
    ]
    path = parts.path
    if detect_ats(url) is ATS.LEVER:
        path = path.removesuffix("/").removesuffix("/apply")
    return urlunsplit((parts.scheme, parts.netloc.lower(), path, urlencode(query), ""))
