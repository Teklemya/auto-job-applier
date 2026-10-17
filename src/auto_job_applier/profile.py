"""Applicant profile: the single source of factual answers (R10, R11).

Every model forbids unknown keys so a typo in profile.yaml fails loudly instead of silently
falling back to a default (a misspelled sponsorship key must never become a wrong answer).
"""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from . import paths

DECLINE = "Decline to self-identify"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Contact(_Strict):
    first_name: str
    last_name: str
    preferred_name: str | None = None
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    phone: str
    phone_device_type: str = "Mobile"


class Address(_Strict):
    line1: str
    line2: str = ""
    city: str
    state: str
    postal_code: str
    country: str = "United States"


class Links(_Strict):
    linkedin: str | None = None
    github: str | None = None
    portfolio: str | None = None


class WorkAuthorization(_Strict):
    # No defaults on purpose: these answers go out on every application.
    authorized_to_work_us: bool
    requires_sponsorship_now: bool
    requires_sponsorship_future: bool
    visa_status: str | None = None  # e.g. "F-1 OPT"; only used if a form asks directly


class Education(_Strict):
    school: str
    degree: str  # e.g. "Bachelor of Science"
    major: str
    graduation: str  # "YYYY-MM"
    gpa: str | None = None


class Experience(_Strict):
    current_company: str | None = None
    current_title: str | None = None
    years_experience: float = 0


class Demographics(_Strict):
    """Voluntary self-identification (EEO) answers."""

    gender: str = DECLINE
    race_ethnicity: str = DECLINE
    hispanic_latino: str = DECLINE
    veteran_status: str = DECLINE
    disability_status: str = DECLINE


class Preferences(_Strict):
    start_date: str = "Two weeks after offer"
    heard_about_us: str = "Job board"
    salary_floor: int = 120_000  # never answer below this (R12)
    willing_to_relocate: bool = True


class Targeting(_Strict):
    """Location preferences for screening (R6). Edit freely in profile.yaml.

    Entries are "City, ST" (the state must agree when a posting names one, so "Lawrence, MA"
    is not "Lawrence, KS") or a bare alias like "SF" that must equal a posting's city exactly.
    """

    kc_metro: list[str] = [
        "Kansas City, MO",
        "Kansas City, KS",
        "Overland Park, KS",
        "Olathe, KS",
        "Lenexa, KS",
        "Shawnee, KS",
        "Leawood, KS",
        "Lawrence, KS",
        "Lee's Summit, MO",
        "Independence, MO",
    ]
    # Any company in these US tech hubs. Not the clearance-heavy Fort Meade / Annapolis
    # Junction corridor in Maryland (government roles the owner skips).
    hubs: list[str] = [
        # Bay Area
        "San Francisco, CA", "SF", "Bay Area", "Palo Alto, CA", "Mountain View, CA",
        "Sunnyvale, CA", "San Jose, CA", "Santa Clara, CA", "Cupertino, CA", "Menlo Park, CA",
        "Redwood City, CA", "San Mateo, CA", "Foster City, CA", "Burlingame, CA",
        "South San Francisco, CA", "Fremont, CA", "Milpitas, CA", "Oakland, CA",
        # Seattle
        "Seattle, WA", "Bellevue, WA", "Redmond, WA", "Kirkland, WA",
        # New York
        "New York, NY", "NYC", "New York City", "Brooklyn, NY", "Jersey City, NJ",
        "Hoboken, NJ",
        # Boston
        "Boston, MA", "Cambridge, MA",
        # DMV
        "Washington, DC", "DC", "Arlington, VA", "Alexandria, VA", "McLean, VA", "Tysons, VA",
        "Vienna, VA", "Fairfax, VA", "Chantilly, VA", "Reston, VA", "Herndon, VA",
        "Bethesda, MD", "Rockville, MD",
        # Other major hubs
        "Los Angeles, CA", "LA", "Santa Monica, CA", "Irvine, CA", "San Diego, CA",
        "Austin, TX", "Dallas, TX", "Plano, TX", "Richardson, TX", "Irving, TX", "Frisco, TX",
        "Houston, TX", "Chicago, IL", "Denver, CO", "Boulder, CO", "Centennial, CO",
        "Broomfield, CO", "Atlanta, GA", "Alpharetta, GA", "Raleigh, NC", "Durham, NC",
        "Charlotte, NC", "Pittsburgh, PA", "Philadelphia, PA", "Salt Lake City, UT", "Lehi, UT",
        "Portland, OR", "Miami, FL", "Minneapolis, MN", "Phoenix, AZ", "Scottsdale, AZ",
        "Tempe, AZ",
    ]  # fmt: skip


FitVerdict = Literal["strong", "ok", "stretch", "mismatch"]


class Settings(_Strict):
    # S1: shadow mode. Only the owner flips this by editing profile.yaml.
    submit: bool = False
    daily_submit_cap: int = Field(default=3, ge=0)
    # D9: fit verdicts that skip a job. Add "stretch" to apply less widely.
    fit_skip: list[FitVerdict] = ["mismatch"]


Thinking = Literal["minimal", "low", "medium", "high"]


class FitModel(_Strict):
    """Fit checks: many jobs, simple judgment. Flash-Lite free tier: 500/day, 15/minute."""

    model: str = "gemini-3.5-flash-lite"
    min_interval_seconds: float = Field(default=4.5, ge=0)
    daily_call_budget: int = Field(default=450, ge=0)
    thinking: Thinking | None = "minimal"


class AnswersModel(_Strict):
    """Form answers and cover letters: rare, quality matters. Flash free tier: 20/day, 5/min."""

    model: str = "gemini-3.8-flash"
    min_interval_seconds: float = Field(default=12.5, ge=0)
    daily_call_budget: int = Field(default=18, ge=0)
    thinking: Thinking | None = "low"


class LLMSettings(_Strict):
    """D7/D10: Gemini free tier. Quotas are per model and change, so they live here, not in
    code. Check your project's real limits at https://ai.dev/rate-limit."""

    fit: FitModel = FitModel()
    answers: AnswersModel = AnswersModel()
    fit_batch_size: int = Field(default=8, ge=1, le=20)  # jobs judged per fit call


class Profile(_Strict):
    contact: Contact
    address: Address
    links: Links = Links()
    work_authorization: WorkAuthorization
    education: list[Education] = []
    experience: Experience = Experience()
    demographics: Demographics = Demographics()
    preferences: Preferences = Preferences()
    targeting: Targeting = Targeting()
    settings: Settings = Settings()
    llm: LLMSettings = LLMSettings()
    resume_path: Path

    @field_validator("resume_path")
    @classmethod
    def _resume_exists(cls, v: Path) -> Path:
        v = v.expanduser()
        if not v.is_absolute():  # relative paths live next to profile.yaml
            v = paths.home() / v
        if not v.is_file():
            raise ValueError(f"file not found: {v}")
        return v


class ProfileError(Exception):
    pass


def _read_yaml(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        raise ProfileError(f"{path} is not valid YAML: {e}") from e
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ProfileError(
            f"{path} must be a YAML mapping (key: value), not a {type(data).__name__}"
        )
    return data


def _validate[M: BaseModel](model: type[M], data: dict, path: Path, prefix: str = "") -> M:
    try:
        return model.model_validate(data)
    except ValidationError as e:
        lines = [f"Invalid profile {path}:"]
        for err in e.errors():
            loc = ".".join(str(p) for p in err["loc"])
            lines.append(f"  {prefix}{loc}: {err['msg']}")
        raise ProfileError("\n".join(lines)) from e


def load_profile(path: Path) -> Profile:
    if not path.is_file():
        raise ProfileError(f"Profile not found: {path} (run `aja init`)")
    return _validate(Profile, _read_yaml(path), path)


def load_targeting(path: Path) -> Targeting:
    """Just the screening preferences, so collecting works before the profile is complete."""
    if not path.is_file():
        return Targeting()
    return _validate(Targeting, _read_yaml(path).get("targeting") or {}, path, "targeting.")
