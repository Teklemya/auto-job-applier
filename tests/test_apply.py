import json
from contextlib import contextmanager

import pytest
import yaml
from conftest import FIXTURES
from typer.testing import CliRunner

from auto_job_applier import apply, cli, fit
from auto_job_applier.answers import resolver
from auto_job_applier.answers.bank import AnswerBank
from auto_job_applier.engines import greenhouse
from auto_job_applier.engines.base import PageField, SubmitResult
from auto_job_applier.models import ATS, FormField, Job, Status
from auto_job_applier.profile import load_profile
from auto_job_applier.store import JobStore

LYFT = Job(
    url="https://job-boards.greenhouse.io/embed/job_app?for=lyft&token=8896679002",
    company="Lyft",
    role="Software Engineer",
    ats=ATS.GREENHOUSE,
)


@pytest.fixture
def store(tmp_path):
    s = JobStore(tmp_path / "jobs.db")
    yield s
    s.close()


@pytest.fixture
def profile(example_profile_data, write_profile):
    return load_profile(write_profile(example_profile_data))


class UnsureLLM:
    """Answers every question it gets, never confidently."""

    def __init__(self):
        self.calls = 0

    def generate(self, *, schema, prompt, **kw):
        self.calls += 1
        ids = [int(line[1 : line.index("]")]) for line in prompt.splitlines() if line[:1] == "["]
        return resolver.FieldAnswers(
            answers=[resolver.FieldAnswer(id=i, value="?", confident=False, reason="") for i in ids]
        )


def lyft_fields():
    html = (FIXTURES / "greenhouse" / "lyft.html").read_text()
    api = json.loads((FIXTURES / "greenhouse" / "lyft_api.json").read_text())
    return greenhouse.extract_fields(html, api)


def packet(profile, store, fields=None):
    return apply.build_packet(
        lyft_fields() if fields is None else fields,
        profile,
        bank=AnswerBank(store),
        llm=UnsureLLM(),
        job=LYFT,
        jd="Build rides.",
    )


def answer_for(p, dom_id):
    return next(a for pf, a in zip(p.fields, p.answers, strict=True) if pf.dom_id == dom_id)


def test_contact_details_and_resume_come_from_the_profile(profile, store):
    p = packet(profile, store)
    assert answer_for(p, "first_name").value == "Jane"
    assert answer_for(p, "email").value == "jane.doe@example.com"
    assert answer_for(p, "resume").value == str(profile.resume_path)


def test_eeo_answers_use_the_forms_decline_option(profile, store):
    assert answer_for(packet(profile, store), "gender").value == "Decline To Self Identify"


def test_required_fields_the_profile_cant_answer_block_the_submit(profile, store):
    p = packet(profile, store)
    assert "Employment: Start date month" in p.blockers
    assert "First Name" not in p.blockers


def test_optional_fields_never_block(profile, store):
    p = packet(profile, store)
    assert "LinkedIn Profile" not in p.blockers
    assert "Please share your gender pronouns." not in p.blockers


def test_a_page_without_a_form_is_blocked(profile, store):
    p = packet(profile, store, fields=[])
    assert p.blockers == [apply.NO_FORM]


# --- staging a job (T7: drafts only, never a submit) -----------------------------------------


class FakeSession:
    """A form page: fills everything except `cant_fill`, records screenshots.

    Like the real form, filling a field can reveal more fields (`reveals`) or hide some
    (`hides`), e.g. "Current role" hides the end date.
    """

    def __init__(
        self, fields, cant_fill=(), page_blockers=(), reveals=None, hides=None, submits=None
    ):
        self._fields = fields
        self.submits = submits  # the SubmitResult a click on Submit gives
        self.submitted = 0
        self.picked = {}
        self.offered = {}
        self.cant_fill = set(cant_fill)
        self._page_blockers = list(page_blockers)
        self.reveals = reveals or {}
        self.hides = hides or {}
        self.filled = []
        self.screenshots = []
        self.empty_on_page = []  # (dom id, label) the page shows required but empty

    def fields(self):
        done = {a.dom_id for a in self.filled}
        shown = list(self._fields)
        for dom_id, extra in self.reveals.items():
            if dom_id in done:
                shown += extra
        hidden = {h for dom_id, hs in self.hides.items() if dom_id in done for h in hs}
        return [pf for pf in shown if pf.dom_id not in hidden]

    def fill(self, plan):
        self.filled += [a for a in plan if a.dom_id not in self.cant_fill]
        return [a.dom_id for a in plan if a.dom_id in self.cant_fill]

    def page_blockers(self):
        return self._page_blockers

    def missing_required(self):
        return self.empty_on_page

    def screenshot(self, path):
        self.screenshots.append(path)

    def submit(self):
        self.submitted += 1
        return self.submits


def staged(profile, store, tmp_path, session, submit=False):
    store.upsert_many([LYFT])
    job = store.list()[0]
    apply.stage(
        job,
        session,
        store=store,
        profile=profile,
        llm=UnsureLLM(),
        jd="Build rides.",
        shots_dir=tmp_path / "shots",
        submit=submit,
    )
    return store.get(job.id)


def test_staging_saves_the_packet_and_marks_the_job_staged(profile, store, tmp_path):
    job = staged(profile, store, tmp_path, FakeSession(lyft_fields()))
    assert job.status.value == "STAGED"
    assert "Employment: Start date month" in store.get_packet(job.id).blockers


def test_staging_fills_the_confident_answers(profile, store, tmp_path):
    session = FakeSession(lyft_fields())
    staged(profile, store, tmp_path, session)
    assert ("first_name", "Jane") in {(a.dom_id, a.value) for a in session.filled}


def test_staging_keeps_a_screenshot_of_the_filled_form(profile, store, tmp_path):
    session = FakeSession(lyft_fields())
    job = staged(profile, store, tmp_path, session)
    [shot] = session.screenshots
    assert shot.parent == tmp_path / "shots"
    assert shot.name.startswith(f"{job.id}-")


def test_a_field_revealed_by_an_answer_is_answered_too(profile, store, tmp_path):
    race = PageField("race", FormField("Please identify your race", "select", required=True))
    session = FakeSession(lyft_fields(), reveals={"hispanic_ethnicity": [race]})
    job = staged(profile, store, tmp_path, session)
    assert "race" in {a.dom_id for a in session.filled}
    assert "race" in {pf.dom_id for pf in store.get_packet(job.id).fields}


def test_fields_hidden_by_an_answer_no_longer_block(profile, store, tmp_path):
    end_dates = ["end-date-month-0", "end-date-year-0"]
    session = FakeSession(lyft_fields(), hides={"current-role-0_1": end_dates})
    job = staged(profile, store, tmp_path, session)
    blockers = store.get_packet(job.id).blockers
    assert not any("End date" in b for b in blockers)


def test_a_required_field_the_page_rejected_blocks_the_submit(profile, store, tmp_path):
    job = staged(profile, store, tmp_path, FakeSession(lyft_fields(), cant_fill={"degree--0"}))
    assert "could not fill: Education: Degree" in store.get_packet(job.id).blockers


def test_an_optional_field_the_page_rejected_does_not_block(profile, store, tmp_path):
    job = staged(profile, store, tmp_path, FakeSession(lyft_fields(), cant_fill={"school--0"}))
    assert not any("School" in b for b in store.get_packet(job.id).blockers)


def test_a_visible_captcha_blocks_the_submit(profile, store, tmp_path):
    job = staged(profile, store, tmp_path, FakeSession(lyft_fields(), page_blockers=["CAPTCHA"]))
    assert "CAPTCHA" in store.get_packet(job.id).blockers


def test_a_form_with_nothing_blocking_is_staged_for_shadow_mode(profile, store, tmp_path):
    first = next(pf for pf in lyft_fields() if pf.dom_id == "first_name")
    job = staged(profile, store, tmp_path, FakeSession([first]))
    assert job.status.value == "STAGED"
    assert "shadow mode" in job.reason
    assert store.get_packet(job.id).blockers == []


def test_a_blocked_form_says_how_many_fields_need_the_owner(profile, store, tmp_path):
    job = staged(profile, store, tmp_path, FakeSession(lyft_fields()))
    n = len(store.get_packet(job.id).blockers)
    assert job.reason == f"needs review: {n} blocking"


# --- submitting (T10: live mode only, the caller checks the cap) -------------------------------


def first_name_only():
    return [next(pf for pf in lyft_fields() if pf.dom_id == "first_name")]


def test_a_ready_form_is_submitted_and_applied_once_confirmed(profile, store, tmp_path):
    session = FakeSession(first_name_only(), submits=SubmitResult(confirmed=True))
    job = staged(profile, store, tmp_path, session, submit=True)
    assert session.submitted == 1
    assert (job.status, job.reason) == (Status.APPLIED, apply.AUTO_SUBMITTED)
    assert len(session.screenshots) == 2  # the filled form, then the confirmation page


def test_shadow_mode_never_clicks_submit(profile, store, tmp_path):
    session = FakeSession(first_name_only(), submits=SubmitResult(confirmed=True))
    job = staged(profile, store, tmp_path, session, submit=False)
    assert session.submitted == 0
    assert job.status is Status.STAGED


def test_a_blocked_form_is_never_submitted_even_in_live_mode(profile, store, tmp_path):
    session = FakeSession(lyft_fields(), submits=SubmitResult(confirmed=True))
    job = staged(profile, store, tmp_path, session, submit=True)
    assert session.submitted == 0
    assert job.status is Status.STAGED


def test_a_submit_the_page_rejects_becomes_a_draft(profile, store, tmp_path):
    rejected = SubmitResult(False, ["page rejected the submit: Phone is invalid"])
    job = staged(profile, store, tmp_path, FakeSession(first_name_only(), submits=rejected), True)
    assert job.status is Status.STAGED
    assert store.get_packet(job.id).blockers == rejected.problems


def test_no_confirmation_page_fails_the_job_so_it_is_never_sent_twice(profile, store, tmp_path):
    session = FakeSession(first_name_only(), submits=SubmitResult(confirmed=False))
    job = staged(profile, store, tmp_path, session, submit=True)
    assert (job.status, job.reason) == (Status.FAILED, apply.UNCONFIRMED)


# --- aja apply ------------------------------------------------------------------------------

runner = CliRunner()


@pytest.fixture
def home_with_profile(aja_home, example_profile_data):
    aja_home.mkdir(parents=True)
    (aja_home / "profile.yaml").write_text(yaml.safe_dump(example_profile_data))
    return aja_home


def queue(home, job, status=Status.PENDING, reason=None):
    s = JobStore(home / "jobs.db")
    s.upsert_many([job])
    job_id = next(j.id for j in s.list() if j.url == job.url)
    s.set_status(job_id, status, reason)
    s.save_description(job_id, "Build rides.")
    s.close()
    return job_id


def fake_greenhouse(monkeypatch):
    @contextmanager
    def fake_session(job):
        yield FakeSession(lyft_fields())

    monkeypatch.setattr(cli, "_greenhouse_session", fake_session)
    monkeypatch.setattr(cli, "make_llm", lambda *a: UnsureLLM())


def test_apply_stages_a_greenhouse_job_and_lists_what_blocks_it(home_with_profile, monkeypatch):
    job_id = queue(home_with_profile, LYFT)

    @contextmanager
    def fake_session(job):
        yield FakeSession(lyft_fields())

    monkeypatch.setattr(cli, "_greenhouse_session", fake_session)
    monkeypatch.setattr(cli, "make_llm", lambda *a: UnsureLLM())

    result = runner.invoke(cli.app, ["apply", str(job_id)])

    assert result.exit_code == 0, result.output
    assert "STAGED" in result.output
    assert "Employment: Start date month" in result.output
    s = JobStore(home_with_profile / "jobs.db")
    assert s.get(job_id).status.value == "STAGED"
    s.close()


def test_apply_refuses_sites_it_cannot_fill_yet(home_with_profile):
    lever = Job(url="https://jobs.lever.co/acme/1", company="Acme", role="SWE", ats=ATS.LEVER)
    job_id = queue(home_with_profile, lever)
    result = runner.invoke(cli.app, ["apply", str(job_id)])
    assert result.exit_code == 1
    assert "Greenhouse" in result.output


def test_apply_refuses_an_unknown_job(home_with_profile):
    result = runner.invoke(cli.app, ["apply", "999"])
    assert result.exit_code == 1
    assert "No job #999" in result.output


def test_apply_can_redo_a_draft(home_with_profile, monkeypatch):
    job_id = queue(home_with_profile, LYFT, Status.STAGED, "needs review: 3 blocking")
    fake_greenhouse(monkeypatch)
    result = runner.invoke(cli.app, ["apply", str(job_id)])
    assert result.exit_code == 0, result.output


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (Status.APPLIED, "submitted by the owner at review"),
        (Status.SKIPPED, "citizenship required (🇺🇸)"),
        (Status.SKIPPED, "duplicate of #153"),
        (Status.NEW, None),
    ],
)
def test_apply_leaves_jobs_that_are_not_queued_alone(
    home_with_profile, monkeypatch, status, reason
):
    # Re-staging an APPLIED job would hide that it was sent; a SKIPPED one would bypass the
    # filters and the one-application-per-company+role rule.
    job_id = queue(home_with_profile, LYFT, status, reason)
    fake_greenhouse(monkeypatch)
    result = runner.invoke(cli.app, ["apply", str(job_id)])
    assert result.exit_code == 1
    assert status.value in result.output
    s = JobStore(home_with_profile / "jobs.db")
    assert s.get(job_id).status is status
    s.close()


def live(home, data, cap=3):
    data = {
        **data,
        "settings": {**data.get("settings", {}), "submit": True, "daily_submit_cap": cap},
    }
    (home / "profile.yaml").write_text(yaml.safe_dump(data))


def ready_greenhouse(monkeypatch, sessions):
    @contextmanager
    def fake_session(job):
        session = FakeSession(first_name_only(), submits=SubmitResult(confirmed=True))
        sessions.append(session)
        yield session

    monkeypatch.setattr(cli, "_greenhouse_session", fake_session)
    monkeypatch.setattr(cli, "make_llm", lambda *a: UnsureLLM())


def test_apply_in_shadow_mode_only_stages(home_with_profile, monkeypatch):
    job_id = queue(home_with_profile, LYFT)
    sessions = []
    ready_greenhouse(monkeypatch, sessions)
    result = runner.invoke(cli.app, ["apply", str(job_id)])
    assert result.exit_code == 0, result.output
    assert sessions[0].submitted == 0
    assert "STAGED" in result.output


def test_apply_in_live_mode_submits_until_the_daily_cap(
    home_with_profile, example_profile_data, monkeypatch
):
    live(home_with_profile, example_profile_data, cap=1)
    first = queue(home_with_profile, LYFT)
    sessions = []
    ready_greenhouse(monkeypatch, sessions)
    assert "APPLIED" in runner.invoke(cli.app, ["apply", str(first)]).output

    brex = Job(
        url="https://job-boards.greenhouse.io/brex/jobs/1",
        company="Brex",
        role="SWE",
        ats=ATS.GREENHOUSE,
    )
    second = queue(home_with_profile, brex)
    result = runner.invoke(cli.app, ["apply", str(second)])
    assert result.exit_code == 0, result.output
    assert "STAGED" in result.output
    assert [s.submitted for s in sessions] == [1, 0]


def test_a_cap_of_zero_keeps_live_mode_from_submitting(
    home_with_profile, example_profile_data, monkeypatch
):
    live(home_with_profile, example_profile_data, cap=0)
    job_id = queue(home_with_profile, LYFT)
    sessions = []
    ready_greenhouse(monkeypatch, sessions)
    runner.invoke(cli.app, ["apply", str(job_id)])
    assert sessions[0].submitted == 0


# --- aja run ---------------------------------------------------------------------------------


class RunLLM(UnsureLLM):
    """Fit checks say "ok" to every job; form answers are never confident."""

    def generate(self, *, schema, prompt, **kw):
        if schema is fit.FitBatch:
            return fit.FitBatch(
                results=[
                    fit.FitItem(id=i, verdict="ok", score=70, matched_keywords=[], reason="ok")
                    for i in fit.job_ids_in(prompt)
                ]
            )
        return super().generate(schema=schema, prompt=prompt, **kw)


def test_run_collects_screens_and_drafts_the_best_greenhouse_jobs(home_with_profile, monkeypatch):
    sessions = []
    ready_greenhouse(monkeypatch, sessions)
    monkeypatch.setattr(cli, "make_llm", lambda *a: RunLLM())
    monkeypatch.setattr(cli, "fetch_description", lambda job: "Build things in Python.")

    readme = str(FIXTURES / "simplify_readme.md")
    result = runner.invoke(cli.app, ["run", "--file", readme, "--limit", "1"])

    assert result.exit_code == 0, result.output
    assert len(sessions) == 1 and sessions[0].submitted == 0
    assert "Run (shadow mode): 0 submitted, 1 drafts" in result.output
    assert "New today that passed screening:" in result.output
    assert "greenhouse" in result.output


def test_a_browser_error_before_submit_leaves_the_job_queued(home_with_profile, monkeypatch):
    @contextmanager
    def broken(job):
        raise RuntimeError("Target page, context or browser has been closed")
        yield

    monkeypatch.setattr(cli, "_greenhouse_session", broken)
    monkeypatch.setattr(cli, "make_llm", lambda *a: RunLLM())
    monkeypatch.setattr(cli, "fetch_description", lambda job: "Build things in Python.")

    readme = str(FIXTURES / "simplify_readme.md")
    result = runner.invoke(cli.app, ["run", "--file", readme, "--limit", "1"])

    assert result.exit_code == 0, result.output
    assert "1 retried next run" in result.output
    s = JobStore(home_with_profile / "jobs.db")
    assert s.list(status=Status.FAILED) == []
    assert len(s.apply_queue(ATS.GREENHOUSE)) == 2
    s.close()


# --- robustness: type-to-search boxes, clicks that may have been sent -------------------------


def test_the_option_a_search_box_picked_becomes_the_answer(profile, store, tmp_path):
    degree = PageField("degree--0", FormField("Education: Degree", "select", required=True))
    session = FakeSession([degree])
    session.picked = {"degree--0": "Bachelor's Degree"}
    job = staged(profile, store, tmp_path, session)
    [answer] = store.get_packet(job.id).answers
    assert (answer.value, answer.confident) == ("Bachelor's Degree", True)


def test_options_seen_in_a_search_box_are_kept_for_review(profile, store, tmp_path):
    major = PageField("discipline--0", FormField("Education: Discipline", "select", True))
    session = FakeSession([major], cant_fill={"discipline--0"})
    session.offered = {"discipline--0": ["Computer Engineering", "Information Systems"]}
    job = staged(profile, store, tmp_path, session)
    packet = store.get_packet(job.id)
    assert packet.fields[0].field.options == ["Computer Engineering", "Information Systems"]
    assert packet.fields[0].field.typeahead
    assert "could not fill: Education: Discipline" in packet.blockers


class Boom(FakeSession):
    def submit(self):
        raise RuntimeError("navigation interrupted")


def test_a_browser_error_during_submit_fails_the_job_so_it_is_never_resent(
    profile, store, tmp_path
):
    job = staged(profile, store, tmp_path, Boom(first_name_only()), submit=True)
    assert job.status is Status.FAILED
    assert job.reason.startswith(apply.CLICKED)


def test_a_draft_made_after_a_submit_click_is_never_submitted_again(
    home_with_profile, example_profile_data, monkeypatch
):
    live(home_with_profile, example_profile_data)
    reason = apply.CLICKED + "needs review: 1 blocking"
    job_id = queue(home_with_profile, LYFT, Status.STAGED, reason)
    sessions = []
    ready_greenhouse(monkeypatch, sessions)
    result = runner.invoke(cli.app, ["apply", str(job_id)])
    assert result.exit_code == 0, result.output
    assert sessions[0].submitted == 0


def test_a_click_is_remembered_after_a_refill_rewrites_the_reason(
    home_with_profile, example_profile_data, monkeypatch
):
    live(home_with_profile, example_profile_data, cap=3)
    job_id = queue(home_with_profile, LYFT, Status.FAILED, apply.UNCONFIRMED)
    s = JobStore(home_with_profile / "jobs.db")
    s.set_status(job_id, Status.STAGED, "needs review: 1 blocking")  # a later re-fill
    s.close()
    sessions = []
    ready_greenhouse(monkeypatch, sessions)
    result = runner.invoke(cli.app, ["apply", str(job_id)])
    assert result.exit_code == 0, result.output
    assert sessions[0].submitted == 0
    assert "--retry" in result.output and "shadow mode" not in result.output


def test_unconfirmed_clicks_count_toward_the_daily_cap(
    home_with_profile, example_profile_data, monkeypatch
):
    live(home_with_profile, example_profile_data, cap=1)
    first = queue(home_with_profile, LYFT)
    s = JobStore(home_with_profile / "jobs.db")
    s.set_status(first, Status.FAILED, apply.UNCONFIRMED)
    s.close()
    brex = Job(
        url="https://job-boards.greenhouse.io/brex/jobs/1",
        company="Brex",
        role="SWE",
        ats=ATS.GREENHOUSE,
    )
    second = queue(home_with_profile, brex)
    sessions = []
    ready_greenhouse(monkeypatch, sessions)
    result = runner.invoke(cli.app, ["apply", str(second)])
    assert sessions[0].submitted == 0
    assert "today's cap of 1 is used" in result.output


def test_retry_submits_a_click_that_did_not_go_through_and_waits_for_a_code(
    home_with_profile, example_profile_data, monkeypatch
):
    live(home_with_profile, example_profile_data, cap=2)
    job_id = queue(home_with_profile, LYFT, Status.FAILED, apply.UNCONFIRMED)
    sessions = []
    ready_greenhouse(monkeypatch, sessions)

    refused = runner.invoke(cli.app, ["apply", str(job_id)])
    assert refused.exit_code == 1 and sessions == []

    result = runner.invoke(cli.app, ["apply", str(job_id), "--retry"])
    assert result.exit_code == 0, result.output
    assert sessions[0].submitted == 1
    assert sessions[0].code_wait_ms == cli.CODE_WAIT_MS
    assert "APPLIED" in result.output


def test_a_required_box_the_reader_missed_keeps_the_form_a_draft(profile, store, tmp_path):
    session = FakeSession(first_name_only(), submits=SubmitResult(confirmed=True))
    session.empty_on_page = [("ack", "I acknowledge the privacy notice")]
    job = staged(profile, store, tmp_path, session, submit=True)
    assert session.submitted == 0
    assert job.status is Status.STAGED
    assert store.get_packet(job.id).blockers == [
        apply.EMPTY_ON_PAGE + "I acknowledge the privacy notice"
    ]


def test_a_field_already_flagged_is_not_listed_twice(profile, store, tmp_path):
    major = PageField("discipline--0", FormField("Education: Discipline", "select", True))
    session = FakeSession([major], cant_fill={"discipline--0"})
    session.empty_on_page = [("discipline--0", "Discipline")]
    job = staged(profile, store, tmp_path, session)
    assert store.get_packet(job.id).blockers == ["could not fill: Education: Discipline"]


def test_a_page_rejection_was_not_sent_so_it_does_not_lock_the_job(profile, store, tmp_path):
    rejected = SubmitResult(False, [apply.REJECTED + "This field is required."])
    job = staged(profile, store, tmp_path, FakeSession(first_name_only(), submits=rejected), True)
    assert job.status is Status.STAGED
    assert not job.reason.startswith(apply.CLICKED)


def test_a_page_that_cannot_be_checked_is_not_submitted(profile, store, tmp_path):
    class Unreadable(FakeSession):
        def missing_required(self):
            raise RuntimeError("page navigated")

    session = Unreadable(first_name_only(), submits=SubmitResult(confirmed=True))
    job = staged(profile, store, tmp_path, session, submit=True)
    assert (session.submitted, job.status) == (0, Status.STAGED)
