import yaml
from typer.testing import CliRunner

from auto_job_applier.cli import app

runner = CliRunner()


def test_init_creates_home_and_example_profile(aja_home):
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.output
    assert (aja_home / "profile.yaml").exists()
    assert (aja_home / "jobs.db").exists()


def test_init_never_overwrites_an_existing_profile(aja_home):
    aja_home.mkdir(parents=True)
    (aja_home / "profile.yaml").write_text("mine: true\n")
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.output
    assert (aja_home / "profile.yaml").read_text() == "mine: true\n"


def test_stats_on_empty_db(aja_home):
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["stats"])
    assert result.exit_code == 0, result.output
    assert "No jobs yet" in result.output


def test_check_reports_profile_errors(aja_home):
    runner.invoke(app, ["init"])  # example profile has a placeholder resume path
    result = runner.invoke(app, ["check"])
    assert result.exit_code == 1
    assert "resume_path" in result.output


def test_check_passes_with_valid_profile(aja_home, example_profile_data):
    aja_home.mkdir(parents=True)
    (aja_home / "profile.yaml").write_text(yaml.safe_dump(example_profile_data))
    result = runner.invoke(app, ["check"])
    assert result.exit_code == 0, result.output
    assert "shadow mode" in result.output.lower()
