"""`aja` command-line entry point."""

from importlib import resources
from pathlib import Path
from typing import Annotated

import typer

from . import paths
from .fit import screen_fit
from .jd_fetch import fetch_description
from .llm import LLMError, make_llm
from .models import ATS, Status
from .profile import ProfileError, load_profile, load_targeting
from .screen import screen_new
from .sources import simplify
from .store import JobStore

app = typer.Typer(no_args_is_help=True, help="Collect SWE roles and apply to them.")


def _store() -> JobStore:
    return JobStore(paths.db_path())


@app.command()
def init() -> None:
    """Create ~/.auto-job-applier with an example profile and an empty database."""
    home = paths.home()
    home.mkdir(parents=True, exist_ok=True)
    profile = paths.profile_path()
    if profile.exists():
        typer.echo(f"Keeping existing profile: {profile}")
    else:
        example = resources.files("auto_job_applier").joinpath("profile.example.yaml")
        profile.write_text(example.read_text())
        typer.echo(f"Wrote example profile: {profile}  (edit it, then run `aja check`)")
    _store().close()
    typer.echo(f"Database: {paths.db_path()}")


@app.command()
def check() -> None:
    """Validate the profile."""
    try:
        profile = load_profile(paths.profile_path())
    except ProfileError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from e
    mode = "LIVE (submits enabled)" if profile.settings.submit else "shadow mode (drafts only)"
    typer.echo(f"Profile OK: {profile.contact.first_name} {profile.contact.last_name}, {mode}")


@app.command()
def stats() -> None:
    """Job counts by ATS and status."""
    store = _store()
    counts = store.counts()
    store.close()
    if not counts:
        typer.echo("No jobs yet. Run `aja collect`.")
        return
    for (ats, status), n in sorted(counts.items()):
        typer.echo(f"{ats:<11} {status:<8} {n:>5}")


@app.command()
def collect(
    file: Annotated[
        Path | None, typer.Option(help="Parse a local README instead of fetching it.")
    ] = None,
) -> None:
    """Fetch the SimplifyJobs README and add its open SWE roles to the queue."""
    try:
        targeting = load_targeting(paths.profile_path())
        markdown = file.read_text() if file else simplify.fetch_readme()
    except ProfileError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from e
    except OSError as e:  # URLError, timeouts, unreadable --file
        typer.echo(f"Could not fetch SimplifyJobs README: {e}", err=True)
        raise typer.Exit(1) from e
    jobs = simplify.parse_simplify(markdown)
    if not jobs:
        # Never treat an empty parse as "everything closed": the format probably changed.
        typer.echo(
            "Parsed 0 roles from SimplifyJobs; the README format may have changed.", err=True
        )
        raise typer.Exit(1)
    store = _store()
    inserted, updated = store.upsert_many(jobs)
    retired = store.retire_missing("simplify", {j.url for j in jobs})
    counts = screen_new(store, targeting)
    store.close()
    typer.echo(f"SimplifyJobs: {len(jobs)} open SWE roles ({inserted} new, {updated} updated)")
    if retired:
        typer.echo(f"Retired {retired} postings no longer listed")
    if counts:
        summary = ", ".join(f"{n} {s}" for s, n in sorted(counts.items()))
        typer.echo(f"Screened {sum(counts.values())}: {summary}")


@app.command()
def fit(
    limit: Annotated[int | None, typer.Option(help="Check at most this many new jobs.")] = None,
) -> None:
    """Fetch job descriptions and run the LLM fit check on queued jobs (free Gemini tier)."""
    store = _store()
    try:
        profile = load_profile(paths.profile_path())
        run = screen_fit(store, profile, make_llm(profile, store), fetch_description, limit)
    except (ProfileError, LLMError) as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from e
    finally:
        store.close()
    for job, f in run.results:
        verdict = f"{f.verdict:>8} {f.score:>3}" if f.verdict else f"{'-':>8} {'-':>3}"
        typer.echo(f"{job.id:>5} {verdict}  {job.company[:20]:<20} {job.role[:40]:<40}  {f.reason}")
    summary = ", ".join(f"{n} {k}" for k, n in sorted(run.counts.items()) if k != "checked")
    typer.echo(f"Fit checked {run.counts['checked']}" + (f" ({summary})" if summary else ""))
    if run.stopped:
        typer.echo(f"Stopped early: {run.stopped}. Remaining jobs wait for the next run.")


@app.command("list")
def list_jobs(
    status: Annotated[Status | None, typer.Option(case_sensitive=False)] = None,
    ats: Annotated[ATS | None, typer.Option(case_sensitive=False)] = None,
    limit: int = 50,
) -> None:
    """List queued jobs."""
    store = _store()
    jobs = store.list(status=status, ats=ats, limit=limit)
    store.close()
    for j in jobs:
        flags = f" [{', '.join(j.flags)}]" if j.flags else ""
        reason = f"  ({j.reason})" if j.reason else ""
        typer.echo(
            f"{j.id:>5}  {j.status:<8} {j.ats:<10} {j.company[:24]:<24} "
            f"{j.role[:44]:<44} {j.location[:30]}{flags}{reason}"
        )
