"""Filesystem layout.

Personal data lives under AJA_HOME (default ~/.auto-job-applier), never in the repo.
"""

import os
from pathlib import Path


def home() -> Path:
    return Path(os.environ.get("AJA_HOME", "~/.auto-job-applier")).expanduser()


def db_path() -> Path:
    return home() / "jobs.db"


def profile_path() -> Path:
    return home() / "profile.yaml"


def browser_profile_dir() -> Path:
    """Persistent Chrome user-data dir; keeps ATS and Jobright logins between runs."""
    return home() / "browser-profile"


def artifacts_dir() -> Path:
    """Screenshots and answer packets saved for every submit and draft."""
    return home() / "artifacts"
