"""Applicant profile: the single source of factual answers (R10, R11).

Every model forbids unknown keys so a typo in profile.yaml fails loudly instead of silently
falling back to a default (a misspelled sponsorship key must never become a wrong answer).
"""

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)

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
    # Countries you are a citizen of, for "are you a citizen or resident of Cuba, Iran, ...?"
    # (export-control screening). Residence comes from the address. Left empty, such
    # questions go to review.
    citizenships: list[str] = []
    # Exact option sentences the owner approved for questions whose options aren't Yes/No,
    # e.g. "I am authorized to work for any employer in the country ...". Used verbatim.
    approved_answers: list[str] = []
    # The owner's own words for open-ended text questions ("what sponsorship would you
    # require?", "describe your work authorization"). Never used for dropdowns or citizenship.
    explanation: str | None = None


_DATE_FORMATS = [
    (re.compile(r"^(\d{4})[-/](\d{1,2})(?:[-/]\d{1,2})?$"), lambda m: (m[1], m[2])),  # 2022-08(-21)
    (re.compile(r"^(\d{1,2})[-/]\d{1,2}[-/](\d{4})$"), lambda m: (m[2], m[1])),  # 08-21-2022
    (re.compile(r"^(\d{1,2})[-/](\d{4})$"), lambda m: (m[2], m[1])),  # 08/2022
    (re.compile(r"^(\d{1,2})[-/](\d{2})$"), lambda m: ("20" + m[2], m[1])),  # 08-22
]


_MONTH_NAME = re.compile(r"^([a-z]{3,9})\.?,? (\d{4})$|^(\d{4}),? ([a-z]{3,9})\.?$", re.I)
_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]


def to_year_month(text: str) -> str:
    """A date in any common order, as "YYYY-MM"; a bare year stays "YYYY" (month unknown)."""
    text = text.strip()
    if re.fullmatch(r"\d{4}", text):
        return text
    if m := _MONTH_NAME.match(text):
        name, year = (m[1], m[2]) if m[1] else (m[4], m[3])
        if name[:3].lower() in _MONTHS:
            return f"{year}-{_MONTHS.index(name[:3].lower()) + 1:02d}"
    for pattern, parts in _DATE_FORMATS:
        if m := pattern.match(text):
            year, month = parts(m)
            if 1 <= int(month) <= 12:
                return f"{year}-{int(month):02d}"
    raise ValueError(f"{text!r} is not a date like 2022-08, 2022-08-21, 08/2022 or May 2022")


class Education(_Strict):
    school: str
    degree: str  # e.g. "Bachelor of Science"
    major: str
    # Other spellings or close choices, tried in order when a form's dropdown doesn't offer
    # the value above, e.g. degree_also: ["Bachelor's Degree", "Bachelor"]. The LLM only ever
    # sees the values above.
    school_also: list[str] = []
    degree_also: list[str] = []
    major_also: list[str] = []
    # Dates are stored as "YYYY-MM"; "2022-08-21", "08/2022", "08-21-2022", "08-22" and
    # "Aug 2022" work too. A bare year ("2024") is kept, and month questions go to review.
    start: str | None = Field(None, validation_alias=AliasChoices("start", "start_date"))
    graduation: str = Field(validation_alias=AliasChoices("graduation", "end_date"))
    gpa: str | None = None

    @field_validator("start", "graduation", mode="before")
    @classmethod
    def _year_month(cls, v: object) -> object:
        return None if v is None else to_year_month(str(v))


class Experience(_Strict):
    current_company: str | None = None
    current_title: str | None = None
    years_experience: float = 0
    # Companies you have worked for before, for "have you previously worked at X?" questions.
    past_employers: list[str] = []
    # Companies where immediate family works, for "are any of your relatives employees of X?".
    family_employed_at: list[str] = []
    # "Are you subject to a non-compete or non-solicitation agreement?" Left empty, it goes
    # to review.
    non_compete: bool | None = None


class Demographics(_Strict):
    """Voluntary self-identification (EEO) answers."""

    # Each takes one answer, or spellings tried in order until the form offers one:
    # veteran_status: ["I am not a protected veteran", "No military service"].
    gender: str | list[str] = DECLINE
    race_ethnicity: str | list[str] = DECLINE
    hispanic_latino: str | list[str] = DECLINE
    veteran_status: str | list[str] = DECLINE
    disability_status: str | list[str] = DECLINE
    sexual_orientation: str | list[str] = DECLINE
    transgender: str | list[str] = DECLINE  # "Do you identify as transgender?"
    pronouns: str | None = None  # e.g. "He/Him"; left empty, a required question goes to review


class Preferences(_Strict):
    start_date: str = "Two weeks after offer"
    # The first month you could start ("2027-01"), for "are you able to start between January
    # and September 2027?". Left empty, such questions go to review.
    earliest_start: str | None = None
    # One answer, or choices tried in order until the form offers one.
    heard_about_us: str | list[str] = ["Job board", "Online job board", "Job posting", "Other"]
    salary_floor: int = 120_000  # never answer below this (R12)
    willing_to_relocate: bool = True
    # "Would you be able to work hybrid / in the office?" Left empty, it goes to review.
    in_office_ok: bool | None = None
    # Ranked choices for "what type of engineering are you interested in?" and "preferred
    # office location?": the first one a form offers is picked.
    engineering_focus: list[str] = []
    office_locations: list[str] = []
    # Tick "I consent to <company> processing my demographic survey answers". Left empty,
    # a required consent box goes to review.
    demographic_consent: bool | None = None
    # Tick required boxes that only acknowledge or consent ("I have read the privacy notice").
    # Boxes that state a fact about you ("I certify I am a citizen") are never ticked.
    tick_acknowledgements: bool = True

    @field_validator("earliest_start", mode="before")
    @classmethod
    def _year_month(cls, v: object) -> object:
        return None if v is None else to_year_month(str(v))


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
