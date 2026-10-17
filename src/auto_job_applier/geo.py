"""US location recognition for screening.

Everything here is an allowlist: a location counts as US only with positive evidence (a state,
"USA", or a configured US city). There is no list of foreign places to keep up to date.
"""

import re
from urllib.parse import urlsplit

US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan",
    "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey",
    "NM": "New Mexico", "NY": "New York", "NC": "North Carolina", "ND": "North Dakota",
    "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
    "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee",
    "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}  # fmt: skip
_STATE_BY_NAME = {name.lower(): abbr for abbr, name in US_STATES.items()}
US_COUNTRY = {"us", "usa", "u.s.", "u.s.a.", "united states", "united states of america"}

_REMOTE = re.compile(r"\bremote\b", re.I)
_REMOTE_FILLER = re.compile(r"\b(in|within|from|only|anywhere|based)\b", re.I)


def _norm(text: str) -> str:
    return " ".join(text.split()).strip().lower()


def state_of(text: str) -> str | None:
    """'KS' or 'Kansas' -> 'KS'; anything else -> None."""
    t = _norm(text).rstrip(".")
    if t.upper() in US_STATES:
        return t.upper()
    return _STATE_BY_NAME.get(t)


def is_us_country(text: str) -> bool:
    return _norm(text) in US_COUNTRY


def is_us_region(text: str) -> bool:
    return is_us_country(text) or state_of(text) is not None


def is_remote_us(loc: str) -> bool:
    """'Remote', 'Remote in USA', 'Remote - US', 'Remote, CA': yes. 'Remote - Portugal': no."""
    if not _REMOTE.search(loc):
        return False
    rest = _REMOTE_FILLER.sub(" ", _REMOTE.sub(" ", loc))
    rest = _norm(re.sub(r"[-–—(),/:|]", " ", rest))
    return rest == "" or is_us_region(rest)


def place_matches(loc: str, entry: str) -> bool:
    """Match a location against a configured place like 'Olathe, KS' or an alias like 'SF'.

    The city part must be equal. If both name a state, the states must agree, so
    'Lawrence, MA' doesn't match 'Lawrence, KS' and 'New Orleans, LA' never matches alias 'LA'.
    """
    city, *regions = [p.strip() for p in loc.split(",")]
    entry_city, *entry_regions = [p.strip() for p in entry.split(",")]
    if _norm(city) != _norm(entry_city):
        return False
    if not regions or not entry_regions:
        return True
    # "Cambridge, UK" has no US state, so it can't equal "Cambridge, MA".
    return state_of(entry_regions[0]) in {state_of(r) for r in regions}


def workday_remote_us(url: str) -> bool:
    """Workday puts the location in the segment after /job/, e.g. /job/Remote-California/<title>."""
    segments = urlsplit(url).path.split("/")
    if "job" not in segments:
        return False
    i = segments.index("job")
    if i + 1 >= len(segments):
        return False
    location = re.sub(r"[-_]+", " ", segments[i + 1])
    return location.lower().startswith("remote") and is_remote_us(location)
