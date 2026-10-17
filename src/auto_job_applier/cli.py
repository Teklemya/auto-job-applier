"""`aja` command-line entry point."""

from importlib import resources

import typer

from . import paths
from .profile import ProfileError, load_profile
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
