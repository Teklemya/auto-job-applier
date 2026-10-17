"""Applicant profile: the single source of factual answers (R10, R11).

Every model forbids unknown keys so a typo in profile.yaml fails loudly instead of silently
falling back to a default (a misspelled sponsorship key must never become a wrong answer).
"""

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

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
    """Location and company preferences for screening (R6)."""

    big_companies: list[str] = []  # owner-editable "well-known" list, beyond 🔥 FAANG+
    kc_metro: list[str] = [
        "Kansas City",
        "Overland Park",
        "Olathe",
        "Lenexa",
        "Lee's Summit",
        "Independence",
        "Shawnee",
        "Leawood",
        "Lawrence",
    ]
    hubs: list[str] = [
        "San Francisco",
        "SF",
        "Bay Area",
        "Santa Clara",
        "Cupertino",
        "Redwood City",
        "Palo Alto",
        "Mountain View",
        "Sunnyvale",
        "San Jose",
        "Menlo Park",
        "Seattle",
        "Bellevue",
        "Redmond",
        "New York",
        "NYC",
        "Boston",
        "Cambridge",
        "Washington, DC",
        "Arlington, VA",
        "McLean",
        "Reston",
        "Bethesda",
    ]


class Settings(_Strict):
    # S1: shadow mode. Only the owner flips this by editing profile.yaml.
    submit: bool = False
    daily_submit_cap: int = Field(default=3, ge=0)


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
    resume_path: Path

    @field_validator("resume_path")
    @classmethod
    def _resume_exists(cls, v: Path) -> Path:
        v = v.expanduser()
        if not v.is_file():
            raise ValueError(f"file not found: {v}")
        return v


class ProfileError(Exception):
    pass


def _read_yaml(path: Path) -> dict:
    try:
        return yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        raise ProfileError(f"{path} is not valid YAML: {e}") from e


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
