"""`aja` command-line entry point."""

from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from importlib import resources
from pathlib import Path
from typing import Annotated

import typer

from . import browser, inbox, paths, review
from .answers.bank import AnswerBank
from .answers.resolver import is_work_auth
from .answers.writer import make_writer
from .apply import CLICKED, stage
from .engines import greenhouse
from .engines.base import Packet, fill_plan
from .fit import FitRun, screen_fit
from .jd_fetch import FetchError, PostingClosed, Unsupported, fetch_description
from .llm import LLM, LLMError, make_llm
from .models import ATS, FormField, Job, Status
from .profile import Profile, ProfileError, Targeting, load_profile, load_targeting
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


@app.command("login-mail")
def login_mail() -> None:
    """Store a Gmail app password in the Keychain so submits can read Greenhouse's emailed
    security codes. Create one at https://myaccount.google.com/apppasswords."""
    try:
        account = load_profile(paths.profile_path()).contact.email
    except ProfileError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from e
    password = typer.prompt(f"App password for {account}", hide_input=True).replace(" ", "")
    try:
        inbox.GmailCodes(account, password).check()
    except Exception as e:
        typer.echo(f"Gmail refused the sign-in ({e}). Nothing was saved.", err=True)
        raise typer.Exit(1) from e
    inbox.save_app_password(account, password)
    typer.echo(f"Saved in the Keychain (service {inbox.KEYCHAIN_SERVICE}). Codes are read only.")


@app.command("mail-check")
def mail_check(
    company: str,
    since: Annotated[
        datetime | None, typer.Option(help="Only mail after this time (default: 2 days ago).")
    ] = None,
) -> None:
    """Show what the code reader finds in Gmail for a company, and why it skips each email."""
    try:
        account = load_profile(paths.profile_path()).contact.email
    except ProfileError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from e
    password = inbox.app_password(account)
    if password is None:
        typer.echo(f"No app password in the Keychain for {account}. Run `aja login-mail`.")
        raise typer.Exit(1)
    when = (since or datetime.now() - timedelta(days=2)).astimezone()
    try:
        lines = inbox.GmailCodes(account, password).explain(company, when)
    except Exception as e:
        typer.echo(f"Gmail refused the sign-in or the search: {e}", err=True)
        raise typer.Exit(1) from e
    typer.echo(f"Signed in as {account}. Greenhouse mail since {when:%Y-%m-%d %H:%M}:")
    for line in lines:
        typer.echo(f"  {line}")


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


def _gather(fetch: Callable[[], list[Job]], keep_going: bool) -> list[Job] | None:
    """A source's jobs. With `keep_going`, a failed source is reported and skipped (None)."""
    try:
        return fetch()
    except typer.Exit:
        if not keep_going:
            raise
        return None
    except Exception as e:  # e.g. a browser that won't start: one source, not the whole run
        if not keep_going:
            raise
        typer.echo(f"Skipped a source: {e}", err=True)
        return None


def _collect_into(
    store: JobStore,
    targeting: Targeting,
    file: Path | None,
    source: Source,
    keep_going: bool = False,
) -> None:
    use_simplify = file is not None or source in (Source.ALL, Source.SIMPLIFY)
    use_jobright = file is None and source in (Source.ALL, Source.JOBRIGHT)
    simplify_jobs = _gather(lambda: _simplify_jobs(file), keep_going) if use_simplify else None
    jobright_jobs = _gather(_jobright_jobs, keep_going) if use_jobright else None
    if simplify_jobs is not None:
        inserted, updated = store.upsert_many(simplify_jobs)
        retired = store.retire_missing("simplify", {j.url for j in simplify_jobs})
        typer.echo(
            f"SimplifyJobs: {len(simplify_jobs)} open SWE roles ({inserted} new, {updated} updated)"
        )
        if retired:
            typer.echo(f"Retired {retired} postings no longer listed")
    if jobright_jobs is not None:
        # Jobright's feed is a rolling recommendation list, so nothing is retired from it.
        inserted, updated = store.upsert_many(jobright_jobs)
        typer.echo(
            f"Jobright: {len(jobright_jobs)} roles liked or above "
            f"{jobright.MIN_MATCH:.0f}% match ({inserted} new, {updated} updated)"
        )
    counts = screen_new(store, targeting)
    if counts:
        summary = ", ".join(f"{n} {s}" for s, n in sorted(counts.items()))
        typer.echo(f"Screened {sum(counts.values())}: {summary}")


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
    store = _store()
    try:
        _collect_into(store, targeting, file, source)
    finally:
        store.close()


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
    _fit_summary(run)


def _fit_summary(run: FitRun) -> None:
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
def _greenhouse_session(job: Job, questions: bool = True) -> Iterator[greenhouse.GreenhouseSession]:
    """The job's form in the tool's browser. Reading fields needs the questions API; a re-fill
    from a stored packet doesn't."""
    from playwright.sync_api import sync_playwright

    api = greenhouse.fetch_questions(job) if questions else None
    with sync_playwright() as p:
        context = browser.open_context(p, paths.browser_profile_dir())
        try:
            yield greenhouse.GreenhouseSession.open(context.new_page(), job, api)
        finally:
            context.close()


def _today_start() -> str:
    """Local midnight as a store timestamp (UTC): the daily cap and supply count by your day."""
    midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(UTC).isoformat(timespec="seconds")


def _submits_left(profile: Profile, store: JobStore) -> int:
    """0 in shadow mode (S1); otherwise what today's cap (S2) still allows. Every click on
    Submit counts, confirmed or not, since an unconfirmed one may still have been sent."""
    if not profile.settings.submit:
        return 0
    used = store.count_events(CLICKED, _today_start())
    return max(0, profile.settings.daily_submit_cap - used)


CODE_WAIT_MS = 5 * 60_000  # time to type an emailed security code at `aja apply`


def _codes(profile: Profile) -> inbox.GmailCodes | None:
    """The inbox reader for emailed security codes, once `aja login-mail` stored a password."""
    password = inbox.app_password(profile.contact.email)
    return inbox.GmailCodes(profile.contact.email, password) if password else None


def _apply_one(
    store: JobStore,
    profile: Profile,
    model: LLM,
    job: Job,
    retry: bool = False,
    watched: bool = False,
    codes: inbox.GmailCodes | None = None,
) -> Packet:
    """Fill one Greenhouse job; submit it if live mode and the cap allow, else stage a draft.
    A job whose Submit was clicked before (a security code, a rejected page) may already be
    on its way, so it is only re-filled, unless the owner checked and passes `retry`.
    When `watched` (the owner ran it by hand), the page waits for an emailed security code."""
    assert job.id is not None
    clicked_before = (job.reason or "").startswith(CLICKED) and not retry
    jd = store.get_description(job.id)
    if jd is None:
        jd = fetch_description(job)
        store.save_description(job.id, jd)
    with _greenhouse_session(job) as session:
        session.code_wait_ms = CODE_WAIT_MS if watched else 0
        session.codes = codes
        return stage(
            job,
            session,
            store=store,
            profile=profile,
            llm=model,
            jd=jd,
            shots_dir=paths.artifacts_dir(),
            writer=make_writer(profile, model),
            submit=not clicked_before and _submits_left(profile, store) > 0,
        )


@app.command("apply")
def apply_job(
    job_id: int,
    retry: Annotated[
        bool,
        typer.Option(
            "--retry",
            help="Submit again after a click that did not go through (check your email first).",
        ),
    ] = False,
) -> None:
    """Fill a Greenhouse application. It is submitted only in live mode, within the daily cap,
    when nothing blocks it; otherwise it is saved as a draft."""
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
        if job.status not in _FILLABLE and not (retry and job.status is Status.FAILED):
            # APPLIED was sent; SKIPPED failed a filter or is a duplicate; NEW is unscreened.
            why = f": {job.reason}" if job.reason else ""
            typer.echo(
                f"#{job_id} is {job.status}{why}. Only queued or staged jobs are filled.", err=True
            )
            raise typer.Exit(1)
        profile = load_profile(paths.profile_path())
        if profile.settings.submit:
            typer.echo(
                "If the form asks for an emailed security code, type it in the browser "
                "(the page waits up to 5 minutes)."
            )
        model = make_llm(profile, store, "answers")
        codes = _codes(profile) if profile.settings.submit else None
        packet = _apply_one(store, profile, model, job, retry=retry, watched=True, codes=codes)
        job = store.get(job_id)
    except (ProfileError, LLMError, FetchError, PostingClosed, Unsupported) as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from e
    finally:
        store.close()
    typer.echo(f"#{job_id} {job.company}: {job.role} → {job.status} ({job.reason})")
    for blocker in packet.blockers:
        typer.echo(f"  - {blocker}")


@app.command()
def run(
    limit: Annotated[int, typer.Option(help="Apply to at most this many Greenhouse jobs.")] = 5,
    file: Annotated[
        Path | None,
        typer.Option(help="Parse a local SimplifyJobs README instead of fetching sources."),
    ] = None,
) -> None:
    """One unattended pass: collect, screen, fit-check, then apply to the best Greenhouse jobs.

    A source or job that fails is reported and the run goes on. Submits follow `aja apply`:
    live mode, the daily cap and the gate (D1) decide; everything else becomes a draft.
    """
    since = _today_start()
    try:
        profile = load_profile(paths.profile_path())
        targeting = load_targeting(paths.profile_path())
    except ProfileError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from e
    store = _store()
    outcomes: Counter[str] = Counter()
    try:
        _collect_into(store, targeting, file, Source.ALL, keep_going=True)
        try:
            _fit_summary(
                screen_fit(store, profile, make_llm(profile, store, "fit"), fetch_description)
            )
            model = make_llm(profile, store, "answers")
        except LLMError as e:
            typer.echo(str(e), err=True)
            raise typer.Exit(1) from e
        # One reader for the whole run: a code is never used twice. Submits wait for their
        # code one at a time, so back-to-back jobs at one company can't swap codes.
        codes = _codes(profile) if profile.settings.submit else None
        for job in store.apply_queue(ATS.GREENHOUSE, limit):
            assert job.id is not None
            try:
                _apply_one(store, profile, model, job, codes=codes)
            except PostingClosed as e:
                store.set_status(job.id, Status.SKIPPED, f"posting closed: {e}")
            except (FetchError, Unsupported) as e:
                typer.echo(f"#{job.id} {job.company}: no description ({e}); retried next run")
                outcomes["waiting"] += 1
                continue
            except Exception as e:  # before any Submit click (stage records those): retry later
                typer.echo(f"#{job.id} {job.company}: {e}; stays queued for the next run")
                outcomes["errors"] += 1
                continue
            done = store.get(job.id)
            outcomes[done.status.value] += 1
            typer.echo(f"#{job.id} {done.company}: {done.role} → {done.status} ({done.reason})")
        _run_summary(store, profile, outcomes, since)
    finally:
        store.close()


def _run_summary(store: JobStore, profile: Profile, outcomes: Counter[str], since: str) -> None:
    mode = "live" if profile.settings.submit else "shadow mode"
    typer.echo(
        f"\nRun ({mode}): {outcomes[Status.APPLIED]} submitted, {outcomes[Status.STAGED]} drafts, "
        f"{outcomes[Status.FAILED]} failed, {outcomes[Status.SKIPPED]} closed, "
        f"{outcomes['errors'] + outcomes['waiting']} retried next run"
    )
    new = store.new_since(since)
    supply = ", ".join(f"{ats} {n}" for ats, n in sorted(new.items(), key=lambda kv: -kv[1]))
    typer.echo(f"New today that passed screening: {supply or 'none'}")
    waiting = len(store.apply_queue(ATS.GREENHOUSE))
    typer.echo(f"Greenhouse jobs still queued: {waiting}. Drafts: `aja review`")


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
        typer.echo(pf.field.label)
        for n, option in enumerate(pf.field.options, 1):
            typer.echo(f"  {n}. {option}")
        kind = "number or text" if pf.field.options else "text"
        while True:
            value = typer.prompt(
                f"Answer ({kind}; Enter: fill it in the browser)",
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
        with _greenhouse_session(job, questions=False) as session:
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
