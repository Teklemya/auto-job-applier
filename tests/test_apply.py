import json
from contextlib import contextmanager

import pytest
import yaml
from conftest import FIXTURES
from typer.testing import CliRunner

from auto_job_applier import apply, cli
from auto_job_applier.answers import resolver
from auto_job_applier.answers.bank import AnswerBank
from auto_job_applier.engines import greenhouse
from auto_job_applier.engines.base import PageField
from auto_job_applier.models import ATS, FormField, Job
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

    def __init__(self, fields, cant_fill=(), page_blockers=(), reveals=None, hides=None):
        self._fields = fields
        self.cant_fill = set(cant_fill)
        self._page_blockers = list(page_blockers)
        self.reveals = reveals or {}
        self.hides = hides or {}
        self.filled = []
        self.screenshots = []

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

    def screenshot(self, path):
        self.screenshots.append(path)


def staged(profile, store, tmp_path, session):
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


# --- aja apply ------------------------------------------------------------------------------

runner = CliRunner()


@pytest.fixture
def home_with_profile(aja_home, example_profile_data):
    aja_home.mkdir(parents=True)
    (aja_home / "profile.yaml").write_text(yaml.safe_dump(example_profile_data))
    return aja_home


def queue(home, job):
    s = JobStore(home / "jobs.db")
    s.upsert_many([job])
    job_id = s.list()[0].id
    s.save_description(job_id, "Build rides.")
    s.close()
    return job_id


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
