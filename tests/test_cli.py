import yaml
from conftest import FIXTURES
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


def test_collect_from_file_is_idempotent(aja_home):
    readme = str(FIXTURES / "simplify_readme.md")
    first = runner.invoke(app, ["collect", "--file", readme])
    assert first.exit_code == 0, first.output
    assert "14 new" in first.output

    second = runner.invoke(app, ["collect", "--file", readme])
    assert second.exit_code == 0, second.output
    assert "0 new, 14 updated" in second.output

    listed = runner.invoke(app, ["list", "--ats", "workday"])
    assert listed.exit_code == 0, listed.output
    assert "Dexcom" in listed.output and "Axon" not in listed.output


def test_collect_screens_new_jobs(aja_home):
    result = runner.invoke(app, ["collect", "--file", str(FIXTURES / "simplify_readme.md")])
    assert result.exit_code == 0, result.output
    assert "Screened 14:" in result.output
    pending = runner.invoke(app, ["list", "--status", "pending"])
    assert "NVIDIA" in pending.output  # 🔥 in Santa Clara → big-hub tier


def test_collect_fails_loudly_when_nothing_parses(aja_home, tmp_path):
    empty = tmp_path / "README.md"
    empty.write_text("## Renamed Section\n<table></table>\n")
    result = runner.invoke(app, ["collect", "--file", str(empty)])
    assert result.exit_code == 1
    assert "0 roles" in result.output


def test_collect_reports_network_errors(aja_home, monkeypatch):
    import urllib.error

    from auto_job_applier.sources import simplify

    def boom(*a, **k):
        raise urllib.error.URLError("no network")

    monkeypatch.setattr(simplify, "fetch_readme", boom)
    result = runner.invoke(app, ["collect"])
    assert result.exit_code == 1
    assert "Could not fetch" in result.output
    assert "Traceback" not in result.output


def test_collect_retires_postings_that_disappeared(aja_home, tmp_path):
    readme = (FIXTURES / "simplify_readme.md").read_text()
    runner.invoke(app, ["collect", "--file", str(FIXTURES / "simplify_readme.md")])
    # Drop the NVIDIA rows, as if they closed (🔒 rows carry no link, so they just vanish)
    trimmed = tmp_path / "README.md"
    trimmed.write_text(readme.replace("nvidia.wd5.myworkdayjobs.com", "closed.invalid"))
    result = runner.invoke(app, ["collect", "--file", str(trimmed)])
    assert result.exit_code == 0, result.output
    assert "no longer listed" in result.output


def _collect_and_profile(aja_home, example_profile_data):
    runner.invoke(app, ["collect", "--file", str(FIXTURES / "simplify_readme.md")])
    (aja_home / "profile.yaml").write_text(yaml.safe_dump(example_profile_data))


def test_fit_without_api_key_explains_how_to_set_it(aja_home, example_profile_data, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    _collect_and_profile(aja_home, example_profile_data)
    result = runner.invoke(app, ["fit"])
    assert result.exit_code == 1
    assert "GEMINI_API_KEY" in result.output
    assert "Traceback" not in result.output


def test_fit_prints_each_verdict(aja_home, example_profile_data, monkeypatch):
    from auto_job_applier import cli, fit

    class Model:
        def generate(self, **kw):
            ids = fit.job_ids_in(kw["prompt"])
            return fit.FitBatch(
                results=[
                    fit.FitItem(
                        id=i, verdict="ok", score=70, matched_keywords=["Go"], reason="fine"
                    )
                    for i in ids
                ]
            )

    monkeypatch.setattr(cli, "make_llm", lambda profile, store, role: Model())
    monkeypatch.setattr(cli, "fetch_description", lambda job: "Backend services in Python. " * 200)
    _collect_and_profile(aja_home, example_profile_data)
    result = runner.invoke(app, ["fit", "--limit", "2"])
    assert result.exit_code == 0, result.output
    assert result.output.count("ok  70") == 2
    assert "fine" in result.output
    assert "Fit checked 2" in result.output


def test_write_prints_a_cover_letter_for_a_job(aja_home, example_profile_data, monkeypatch):
    from auto_job_applier import cli
    from auto_job_applier.answers import writer

    prompts = []

    class Model:
        def generate(self, **kw):
            prompts.append(kw["prompt"])
            return writer.Draft(
                text="Dear NVIDIA hiring team, hi.", confident=True, missing_facts=[]
            )

    monkeypatch.setattr(cli, "make_llm", lambda profile, store, role: Model())
    monkeypatch.setattr(cli, "fetch_description", lambda job: "Build GPU drivers in C++.")
    _collect_and_profile(aja_home, example_profile_data)
    job_id = next(
        line.split()[0]
        for line in runner.invoke(app, ["list"]).output.splitlines()
        if "NVIDIA" in line
    )
    result = runner.invoke(app, ["write", job_id])
    assert result.exit_code == 0, result.output
    assert "Dear NVIDIA hiring team, hi." in result.output
    assert "GPU drivers" in prompts[0]

    why = runner.invoke(app, ["write", job_id, "--why"])
    assert why.exit_code == 0, why.output
    assert "Why do you want to work at NVIDIA?" in prompts[1]


def test_write_unknown_job(aja_home, example_profile_data):
    _collect_and_profile(aja_home, example_profile_data)
    result = runner.invoke(app, ["write", "99999"])
    assert result.exit_code == 1
    assert "No job #99999" in result.output


def test_mail_check_takes_a_short_since_time(aja_home, example_profile_data, monkeypatch):
    from auto_job_applier import inbox

    aja_home.mkdir(parents=True)
    (aja_home / "profile.yaml").write_text(yaml.safe_dump(example_profile_data))
    monkeypatch.setattr(inbox, "app_password", lambda account: None)
    result = runner.invoke(app, ["mail-check", "Affirm", "--since", "2026-10-24T02:00"])
    assert "aja login-mail" in result.output  # got past parsing the time
