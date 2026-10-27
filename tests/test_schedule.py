import plistlib
import subprocess
from datetime import datetime

import pytest
from typer.testing import CliRunner

from auto_job_applier import cli, schedule

runner = CliRunner()


@pytest.fixture
def mac(tmp_path, monkeypatch, aja_home):
    """A home folder for LaunchAgents, an `aja` on PATH, and launchctl/osascript recorded."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(schedule.shutil, "which", lambda name: "/Users/me/aja/.venv/bin/aja")
    calls = []

    def run(args, **kw):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(schedule.subprocess, "run", run)
    return calls


@pytest.mark.parametrize(
    "text, expected",
    [("21:30", (21, 30)), ("9:30pm", (21, 30)), ("9pm", (21, 0)), ("12am", (0, 0)),
     ("12:15pm", (12, 15)), ("7", (7, 0))],
)  # fmt: skip
def test_times_of_day(text, expected):
    assert schedule.parse_time(text) == expected


@pytest.mark.parametrize("text", ["25:00", "9:75", "13pm", "tonight"])
def test_bad_times_are_refused(text):
    with pytest.raises(ValueError):
        schedule.parse_time(text)


def test_schedule_installs_a_daily_launchd_job_without_secrets(mac, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "secret-key")
    result = runner.invoke(cli.app, ["schedule", "--at", "9:30pm"])
    assert result.exit_code == 0, result.output
    job = plistlib.loads(schedule.plist_path().read_bytes())
    assert job["StartCalendarInterval"] == {"Hour": 21, "Minute": 30}
    assert job["ProgramArguments"][:2] == ["/bin/zsh", "-ic"]
    assert "'/Users/me/aja/.venv/bin/aja' run --limit 10 --notify" in job["ProgramArguments"][2]
    assert job["StandardOutPath"] == str(schedule.log_path())
    assert "secret-key" not in schedule.plist_path().read_text()
    assert mac[-1][:2] == ["launchctl", "bootstrap"]


def test_rescheduling_replaces_the_job_and_off_removes_it(mac):
    runner.invoke(cli.app, ["schedule", "--at", "21:30"])
    runner.invoke(cli.app, ["schedule", "--at", "07:00"])
    assert schedule.installed_time() == (7, 0)
    assert mac[1][:2] == ["launchctl", "bootout"]
    result = runner.invoke(cli.app, ["schedule", "--off"])
    assert "stopped" in result.output
    assert not schedule.plist_path().exists()
    assert "No daily run" in runner.invoke(cli.app, ["schedule"]).output


def test_last_shows_only_the_newest_run(aja_home):
    log = schedule.log_path()
    log.parent.mkdir(parents=True)
    old = schedule.run_header(datetime(2026, 10, 26, 21, 30))
    new = schedule.run_header(datetime(2026, 10, 27, 21, 30))
    log.write_text(f"{old}\nRun (live): 1 submitted\n{new}\nRun (live): 3 submitted\n")
    output = runner.invoke(cli.app, ["last"]).output
    assert "2026-10-27" in output and "3 submitted" in output
    assert "1 submitted" not in output


def test_a_scheduled_run_notifies_its_summary(monkeypatch):
    shown = []
    monkeypatch.setattr(
        cli, "_run", lambda limit, file, wait_s=0: "Run (live): 3 submitted, 4 drafts"
    )
    monkeypatch.setattr(schedule, "notify", shown.append)
    result = runner.invoke(cli.app, ["run", "--notify"])
    assert result.exit_code == 0, result.output
    assert result.output.startswith(schedule.MARKER)
    assert shown == ["Run (live): 3 submitted, 4 drafts"]


def test_a_scheduled_run_that_stops_still_notifies(monkeypatch):
    shown = []

    def broken(limit, file, wait_s=0):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(cli, "_run", broken)
    monkeypatch.setattr(schedule, "notify", shown.append)
    result = runner.invoke(cli.app, ["run", "--notify"])
    assert result.exit_code == 1
    assert "aja last" in shown[0]


def test_a_notification_never_breaks_quoting():
    calls = []
    schedule.notify('3 "sent"', run=lambda args, **kw: calls.append(args))
    assert calls[0][2] == 'display notification "3 \'sent\'" with title "aja"'


def test_a_second_run_waits_for_the_first(aja_home, monkeypatch):
    import fcntl

    aja_home.mkdir(parents=True)
    with open(aja_home / "apply.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        monkeypatch.setattr(cli, "_run_locked", lambda limit, file: pytest.fail("ran twice"))
        result = runner.invoke(cli.app, ["run"])
    assert result.exit_code == 1
    assert "still going" in result.output


def test_a_job_launchd_never_loads_is_not_left_behind(mac, monkeypatch):
    tries = []

    def busy(args, **kw):
        tries.append(args)
        return subprocess.CompletedProcess(args, 5, "", "Bootstrap failed: 5")

    with pytest.raises(RuntimeError, match="Bootstrap failed"):
        schedule.install(21, 30, run=busy, wait=lambda s: None)
    assert len(tries) == 5
    assert not schedule.plist_path().exists()


def test_reloading_waits_until_launchd_has_let_go(mac):
    answers = iter([5, 5, 0])

    def slow(args, **kw):
        return subprocess.CompletedProcess(args, next(answers), "", "")

    assert schedule.install(21, 30, run=slow, wait=lambda s: None).exists()


def test_a_scheduled_run_waits_out_an_apply_then_gives_up(aja_home, monkeypatch):
    import fcntl

    shown = []
    monkeypatch.setattr(schedule, "notify", shown.append)
    monkeypatch.setattr(cli, "SCHEDULED_LOCK_WAIT_S", 0.3)
    aja_home.mkdir(parents=True)
    with open(aja_home / "apply.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        monkeypatch.setattr(cli, "_run_locked", lambda limit, file: pytest.fail("ran twice"))
        result = runner.invoke(cli.app, ["run", "--notify"])
    assert result.exit_code == 1
    assert "still going" in result.output
    assert "stopped early" in shown[0]
