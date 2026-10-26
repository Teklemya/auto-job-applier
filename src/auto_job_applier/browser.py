"""The tool's Chrome profile (D8, D11).

The owner signs into Jobright and Google once with `aja login`. That runs plain Chrome, not
under automation, because Google refuses sign-in from automated browsers. Later runs open the
same profile through Playwright, so the logins carry over.
"""

import subprocess
from pathlib import Path

CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
# Playwright launches Chrome with these; cookies saved without them can't be read back.
PLAYWRIGHT_STORAGE_FLAGS = ["--use-mock-keychain", "--password-store=basic"]
LOGIN_URLS = ["https://jobright.ai/", "https://accounts.google.com/"]


def login_argv(profile_dir: Path, chrome: Path | None = None) -> list[str]:
    return [
        str(chrome or CHROME),
        f"--user-data-dir={profile_dir}",
        *PLAYWRIGHT_STORAGE_FLAGS,
        "--no-first-run",
        "--no-default-browser-check",
        *LOGIN_URLS,
    ]


def open_login(profile_dir: Path) -> None:
    """Open Chrome on the tool profile and wait until the owner quits it."""
    if not CHROME.exists():
        raise FileNotFoundError(f"Google Chrome not found at {CHROME}")
    profile_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        login_argv(profile_dir), check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )


def open_context(playwright, profile_dir: Path):
    """The tool profile under Playwright: real Chrome, headed, logins from `aja login`."""
    return playwright.chromium.launch_persistent_context(
        profile_dir, channel="chrome", headless=False, no_viewport=True
    )
