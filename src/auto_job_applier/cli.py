"""`aja` command-line entry point."""

from importlib import resources
from pathlib import Path
from typing import Annotated

import typer

from . import paths
from .models import ATS, Status
from .profile import ProfileError, load_profile
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
    markdown = file.read_text() if file else simplify.fetch_readme()
    jobs = simplify.parse_simplify(markdown)
    store = _store()
    inserted, updated = store.upsert_many(jobs)
    store.close()
    typer.echo(f"SimplifyJobs: {len(jobs)} open SWE roles ({inserted} new, {updated} updated)")


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
