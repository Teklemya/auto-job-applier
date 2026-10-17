from importlib import resources
from pathlib import Path

import pytest
import yaml

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def aja_home(tmp_path, monkeypatch):
    """Every test gets an isolated AJA_HOME so nothing touches ~/.auto-job-applier."""
    home = tmp_path / "aja-home"
    monkeypatch.setenv("AJA_HOME", str(home))
    return home


@pytest.fixture
def example_profile_data(tmp_path):
    """The shipped example profile as a dict, with resume_path pointing at a real file."""
    text = resources.files("auto_job_applier").joinpath("profile.example.yaml").read_text()
    data = yaml.safe_load(text)
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 fake")
    data["resume_path"] = str(resume)
    return data


@pytest.fixture
def write_profile(tmp_path):
    def _write(data, name="profile.yaml"):
        path = tmp_path / name
        path.write_text(yaml.safe_dump(data, allow_unicode=True))
        return path

    return _write
