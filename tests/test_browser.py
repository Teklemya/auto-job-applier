from typer.testing import CliRunner

from auto_job_applier import browser
from auto_job_applier.cli import app

runner = CliRunner()


def test_login_opens_the_tool_profile_in_plain_chrome(tmp_path):
    argv = browser.login_argv(tmp_path / "profile", chrome=tmp_path / "chrome")
    assert argv[0] == str(tmp_path / "chrome")
    assert f"--user-data-dir={tmp_path / 'profile'}" in argv


def test_login_profile_uses_the_same_cookie_storage_as_playwright(tmp_path):
    # Playwright launches Chrome with these flags; cookies saved without them can't be read back.
    argv = browser.login_argv(tmp_path / "profile", chrome=tmp_path / "chrome")
    assert "--use-mock-keychain" in argv
    assert "--password-store=basic" in argv


def test_login_is_not_an_automated_browser(tmp_path):
    # Google refuses sign-in from browsers under automation (D11).
    argv = browser.login_argv(tmp_path / "profile", chrome=tmp_path / "chrome")
    assert not any(a.startswith(("--enable-automation", "--remote-debugging")) for a in argv)


def test_login_opens_jobright_and_google_sign_in(tmp_path):
    argv = browser.login_argv(tmp_path / "profile", chrome=tmp_path / "chrome")
    assert any("jobright.ai" in a for a in argv)
    assert any("accounts.google.com" in a for a in argv)


def test_login_command_waits_for_chrome_with_the_tool_profile(aja_home, tmp_path, monkeypatch):
    chrome = tmp_path / "chrome"
    chrome.touch()
    monkeypatch.setattr(browser, "CHROME", chrome)
    launched = []
    monkeypatch.setattr(browser.subprocess, "run", lambda argv, **kw: launched.append(argv))

    result = runner.invoke(app, ["login"])

    assert result.exit_code == 0, result.output
    assert f"--user-data-dir={aja_home / 'browser-profile'}" in launched[0]
    assert (aja_home / "browser-profile").is_dir()


def test_login_command_explains_when_chrome_is_missing(aja_home, tmp_path, monkeypatch):
    monkeypatch.setattr(browser, "CHROME", tmp_path / "no-chrome")
    result = runner.invoke(app, ["login"])
    assert result.exit_code == 1
    assert "Google Chrome" in result.output


def test_login_command_keeps_chrome_logs_out_of_the_terminal(aja_home, tmp_path, monkeypatch):
    chrome = tmp_path / "chrome"
    chrome.touch()
    monkeypatch.setattr(browser, "CHROME", chrome)
    options = []
    monkeypatch.setattr(browser.subprocess, "run", lambda argv, **kw: options.append(kw))

    runner.invoke(app, ["login"])

    assert options[0]["stdout"] is browser.subprocess.DEVNULL
    assert options[0]["stderr"] is browser.subprocess.DEVNULL
