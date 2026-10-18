"""`aja` command-line entry point."""

from collections.abc import Iterator
from contextlib import contextmanager
from enum import StrEnum
from importlib import resources
from pathlib import Path
from typing import Annotated

import typer

from . import browser, paths, review
from .answers.bank import AnswerBank
from .answers.resolver import is_work_auth
from .answers.writer import make_writer
from .apply import stage
from .engines import greenhouse
from .engines.base import Packet, fill_plan
from .fit import screen_fit
from .jd_fetch import FetchError, PostingClosed, Unsupported, fetch_description
from .llm import LLMError, make_llm
from .models import ATS, FormField, Job, Status
from .profile import ProfileError, load_profile, load_targeting
from .screen import screen_new
from .sources import jobright, simplify
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
def login() -> None:
    """Sign into Jobright and Google once in the tool's own Chrome profile."""
    typer.echo(
        "Opening Chrome on the tool profile. Sign into Jobright and Google, "
        "then quit Chrome (Cmd+Q) to finish."
    )
    try:
        browser.open_login(paths.browser_profile_dir())
    except FileNotFoundError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from e
    typer.echo(f"Saved logins in {paths.browser_profile_dir()}")


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


class Source(StrEnum):
    ALL = "all"
    SIMPLIFY = "simplify"
    JOBRIGHT = "jobright"


def _simplify_jobs(file: Path | None) -> list[Job]:
    try:
        markdown = file.read_text() if file else simplify.fetch_readme()
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
    return jobs


def _jobright_jobs() -> list[Job]:
    try:
        payloads = jobright.fetch_responses()
    except jobright.JobrightError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from e
    if not jobright.has_job_list(payloads):
        typer.echo("Jobright returned no job list; the page format may have changed.", err=True)
        raise typer.Exit(1)
    return jobright.parse_responses(payloads)


@app.command()
def collect(
    file: Annotated[
        Path | None,
        typer.Option(help="Parse a local SimplifyJobs README instead of fetching it."),
    ] = None,
    source: Annotated[Source, typer.Option(case_sensitive=False)] = Source.ALL,
) -> None:
    """Add open SWE roles from SimplifyJobs and Jobright to the queue, then screen them."""
    try:
        targeting = load_targeting(paths.profile_path())
    except ProfileError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from e
    use_simplify = file is not None or source in (Source.ALL, Source.SIMPLIFY)
    use_jobright = file is None and source in (Source.ALL, Source.JOBRIGHT)
    simplify_jobs = _simplify_jobs(file) if use_simplify else []
    jobright_jobs = _jobright_jobs() if use_jobright else []

    store = _store()
    try:
        if use_simplify:
            inserted, updated = store.upsert_many(simplify_jobs)
            retired = store.retire_missing("simplify", {j.url for j in simplify_jobs})
            typer.echo(
                f"SimplifyJobs: {len(simplify_jobs)} open SWE roles "
                f"({inserted} new, {updated} updated)"
            )
            if retired:
                typer.echo(f"Retired {retired} postings no longer listed")
        if use_jobright:
            # Jobright's feed is a rolling recommendation list, so nothing is retired from it.
            inserted, updated = store.upsert_many(jobright_jobs)
            typer.echo(
                f"Jobright: {len(jobright_jobs)} roles liked or above "
                f"{jobright.MIN_MATCH:.0f}% match ({inserted} new, {updated} updated)"
            )
        counts = screen_new(store, targeting)
    finally:
        store.close()
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
        run = screen_fit(store, profile, make_llm(profile, store, "fit"), fetch_description, limit)
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


@app.command()
def write(
    job_id: int,
    why: Annotated[bool, typer.Option("--why", help="Write a 'why us' answer instead.")] = False,
) -> None:
    """Print a cover letter (or 'why us' answer) for a job, to review the writing tone."""
    store = _store()
    try:
        job = store.get(job_id)
        if job is None:
            typer.echo(f"No job #{job_id}", err=True)
            raise typer.Exit(1)
        profile = load_profile(paths.profile_path())
        jd = store.get_description(job_id)
        if jd is None:
            jd = fetch_description(job)
            store.save_description(job_id, jd)
        label = f"Why do you want to work at {job.company}?" if why else "Cover Letter"
        field = FormField(label, "textarea", required=True)
        answer = make_writer(profile, make_llm(profile, store, "answers"))(field, job, jd)
    except (ProfileError, LLMError, FetchError, PostingClosed, Unsupported) as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from e
    finally:
        store.close()
    typer.echo(f"#{job.id} {job.company}: {job.role}\n")
    if answer.confident:
        typer.echo(answer.value)
    else:
        typer.echo(f"Not confident, would stage a draft: {answer.reason}")


_FILLABLE = (Status.PENDING, Status.STAGED)


@contextmanager
def _greenhouse_session(job: Job) -> Iterator[greenhouse.GreenhouseSession]:
    from playwright.sync_api import sync_playwright

    api = greenhouse.fetch_questions(job)
    with sync_playwright() as p:
        context = browser.open_context(p, paths.browser_profile_dir())
        try:
            yield greenhouse.GreenhouseSession.open(context.new_page(), job, api)
        finally:
            context.close()


@app.command("apply")
def apply_job(job_id: int) -> None:
    """Fill a Greenhouse application and save it as a draft. Never submits (shadow mode)."""
    store = _store()
    try:
        job = store.get(job_id)
        if job is None:
            typer.echo(f"No job #{job_id}", err=True)
            raise typer.Exit(1)
        if job.ats is not ATS.GREENHOUSE:
            typer.echo(
                f"#{job_id} is on {job.ats}; only Greenhouse can be filled so far.", err=True
            )
            raise typer.Exit(1)
        if job.status not in _FILLABLE:
            # APPLIED was sent; SKIPPED failed a filter or is a duplicate; NEW is unscreened.
            why = f": {job.reason}" if job.reason else ""
            typer.echo(
                f"#{job_id} is {job.status}{why}. Only queued or staged jobs are filled.", err=True
            )
            raise typer.Exit(1)
        profile = load_profile(paths.profile_path())
        model = make_llm(profile, store, "answers")
        jd = store.get_description(job_id)
        if jd is None:
            jd = fetch_description(job)
            store.save_description(job_id, jd)
        with _greenhouse_session(job) as session:
            packet = stage(
                job,
                session,
                store=store,
                profile=profile,
                llm=model,
                jd=jd,
                shots_dir=paths.artifacts_dir(),
                writer=make_writer(profile, model),
            )
        job = store.get(job_id)
    except (ProfileError, LLMError, FetchError, PostingClosed, Unsupported) as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from e
    finally:
        store.close()
    typer.echo(f"#{job_id} {job.company}: {job.role} → {job.status} ({job.reason})")
    for blocker in packet.blockers:
        typer.echo(f"  - {blocker}")


def _review_list(store: JobStore) -> None:
    drafts = store.list(status=Status.STAGED, limit=500)
    manual = store.list(status=Status.MANUAL, limit=500)
    typer.echo(f"Drafts ({len(drafts)}): run `aja review <id>`")
    for j in drafts:
        typer.echo(f"{j.id:>5}  {j.company[:24]:<24} {j.role[:44]:<44} {j.reason or ''}")
    typer.echo(f"\nApply by hand ({len(manual)}):")
    for j in manual:
        typer.echo(f"{j.id:>5}  {j.company[:24]:<24} {j.role[:44]:<44} {j.url}")


def _ask_blockers(packet: Packet, bank: AnswerBank) -> Packet:
    for pf in review.blocking_fields(packet):
        hint = f" [{' / '.join(pf.field.options)}]" if pf.field.options else ""
        while True:
            value = typer.prompt(
                f"{pf.field.label}{hint} (Enter: fill it in the browser)",
                default="",
                show_default=False,
            )
            if not value.strip():
                break
            try:
                packet = review.apply_answers(packet, {pf.dom_id: value.strip()}, bank)
                if is_work_auth(pf.field.label):
                    chosen = packet.answers[packet.fields.index(pf)].value
                    typer.echo(
                        "  Work authorization is never reused from past answers. To answer it "
                        "automatically next time, add this line under work_authorization: "
                        f'approved_answers in profile.yaml:\n    - "{chosen}"'
                    )
                break
            except ValueError as e:
                typer.echo(str(e))
    return packet


@app.command("review")
def review_job(
    job_id: Annotated[int | None, typer.Argument(help="A staged job; omit to list them.")] = None,
) -> None:
    """List drafts and manual jobs, or answer, re-fill and hand-submit one draft (R17, R18)."""
    store = _store()
    try:
        if job_id is None:
            _review_list(store)
            return
        job, packet = store.get(job_id), store.get_packet(job_id)
        if job is None or packet is None:
            typer.echo(f"No draft for #{job_id}", err=True)
            raise typer.Exit(1)
        typer.echo(f"#{job_id} {job.company}: {job.role} ({job.reason})")
        packet = _ask_blockers(packet, AnswerBank(store))
        store.save_packet(job_id, packet)
        with _greenhouse_session(job) as session:
            rejected = session.fill(fill_plan(packet.fields, packet.answers))
            still_open = [pf.dom_id for pf in review.blocking_fields(packet)]
            session.highlight(still_open + [i for i in rejected if i not in still_open])
            typer.echo("Filled. Check the form (blanks are outlined in red) and submit it there.")
            submitted = typer.confirm("Did you submit the application?", default=False)
        if submitted:
            store.set_status(job_id, Status.APPLIED, "submitted by the owner at review")
            typer.echo(f"#{job_id} marked APPLIED")
        else:
            typer.echo(f"#{job_id} stays STAGED")
    finally:
        store.close()


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
