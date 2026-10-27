"""The daily `aja run` on the owner's Mac (T12): one launchd job, a notification, a log.

launchd starts jobs without the login shell's environment, so the run goes through an
interactive zsh: GEMINI_API_KEY comes from ~/.zshrc as in the owner's terminal, and no secret
is ever written into the job file. A run missed while the Mac slept starts when it wakes.
"""

import contextlib
import os
import plistlib
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from . import paths

LABEL = "com.auto-job-applier.run"
RUN_LIMIT = 10
MARKER = "=== aja run "  # starts every scheduled run in the log

Runner = Callable[..., subprocess.CompletedProcess]


def plist_path() -> Path:
    return Path("~/Library/LaunchAgents").expanduser() / f"{LABEL}.plist"


def log_path() -> Path:
    return paths.home() / "logs" / "run.log"


def parse_time(text: str) -> tuple[int, int]:
    """ "21:30", "9:30pm", "9pm" → (hour, minute)."""
    m = re.fullmatch(r"\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*", text.lower())
    if not m:
        raise ValueError(f"{text!r} is not a time like 21:30 or 9:30pm")
    hour, minute, half = int(m[1]), int(m[2] or 0), m[3]
    if half:
        if not 1 <= hour <= 12:
            raise ValueError(f"{text!r}: hour must be 1-12 with am/pm")
        hour = hour % 12 + (12 if half == "pm" else 0)
    if hour > 23 or minute > 59:
        raise ValueError(f"{text!r} is not a time of day")
    return hour, minute


def aja_command() -> str:
    """The `aja` this terminal runs (the project's venv), as an absolute path."""
    found = shutil.which("aja") or str(Path(sys.argv[0]))
    return str(Path(found).resolve())


def job_plist(hour: int, minute: int, aja: str, log: Path) -> bytes:
    env = {"AJA_HOME": os.environ["AJA_HOME"]} if os.environ.get("AJA_HOME") else {}
    job = {
        "Label": LABEL,
        "ProgramArguments": ["/bin/zsh", "-ic", f"'{aja}' run --limit {RUN_LIMIT} --notify"],
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
        **({"EnvironmentVariables": env} if env else {}),
    }
    return plistlib.dumps(job)


def install(
    hour: int, minute: int, run: Runner | None = None, wait: Callable[[float], None] | None = None
) -> Path:
    """Write the job and (re)load it; an earlier schedule is replaced. launchd unloads the old
    job in the background, so loading the new one is retried for a few seconds; if it never
    loads, the job file is removed so `aja schedule` doesn't claim a run that won't happen."""
    run, wait = run or subprocess.run, wait or time.sleep
    log = log_path()
    log.parent.mkdir(parents=True, exist_ok=True)
    target = plist_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    uninstall(run)
    target.write_bytes(job_plist(hour, minute, aja_command(), log))
    load = ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(target)]
    for attempt in range(5):
        done = run(load, check=False, capture_output=True, text=True)
        if done.returncode == 0:
            return target
        wait(1.0 + attempt)
    target.unlink()
    raise RuntimeError(f"launchctl could not load the job: {(done.stderr or '').strip()}")


def uninstall(run: Runner | None = None) -> bool:
    """Unload and delete the job. False when there was none."""
    run = run or subprocess.run
    target = plist_path()
    if not target.exists():
        return False
    run(["launchctl", "bootout", f"gui/{os.getuid()}", str(target)], check=False)
    target.unlink()
    return True


def installed_time() -> tuple[int, int] | None:
    target = plist_path()
    if not target.exists():
        return None
    when = plistlib.loads(target.read_bytes()).get("StartCalendarInterval", {})
    return when.get("Hour", 0), when.get("Minute", 0)


def notify(message: str, run: Runner | None = None) -> None:
    """A macOS notification; quietly nothing elsewhere."""
    run = run or subprocess.run
    text = message.replace("\\", "").replace('"', "'")
    with contextlib.suppress(OSError):  # no osascript: not a Mac
        run(
            ["osascript", "-e", f'display notification "{text}" with title "aja"'],
            check=False,
            capture_output=True,
        )


def run_header(now: datetime | None = None) -> str:
    return f"{MARKER}{(now or datetime.now()):%Y-%m-%d %H:%M} ==="


def last_run(log: Path | None = None) -> str | None:
    """The newest scheduled run's output, from its header on."""
    log = log or log_path()
    if not log.exists():
        return None
    text = log.read_text(errors="replace")
    start = text.rfind(MARKER)
    return text[start:].rstrip() if start >= 0 else None
