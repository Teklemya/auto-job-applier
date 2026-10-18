from contextlib import contextmanager

import pytest
from typer.testing import CliRunner

from auto_job_applier import cli, review
from auto_job_applier.answers.bank import AnswerBank
from auto_job_applier.engines.base import Packet, PageField
from auto_job_applier.models import ATS, FormField, Job, ResolvedAnswer, Status
from auto_job_applier.store import JobStore


@pytest.fixture
def store(tmp_path):
    s = JobStore(tmp_path / "jobs.db")
    yield s
    s.close()


def draft():
    first = FormField("First Name", "text", required=True)
    degree = FormField("Education: Degree", "select", required=True)
    contact = FormField("May we contact your current employer?", "select", True, ["Yes", "No"])
    return Packet(
        fields=[
            PageField("first_name", first),
            PageField("degree--0", degree),
            PageField("question_7", contact),
        ],
        answers=[
            ResolvedAnswer(first, "Jane", "profile", True),
            ResolvedAnswer(degree, "Bachelor of Science", "profile", True),
            ResolvedAnswer(contact, None, "llm", False, "LLM unsure"),
        ],
        blockers=["could not fill: Education: Degree", "May we contact your current employer?"],
    )


def test_questions_to_ask_are_the_blocking_fields():
    asks = review.blocking_fields(draft())
    assert [pf.dom_id for pf in asks] == ["degree--0", "question_7"]


def test_page_problems_are_not_questions():
    packet = draft()
    packet.blockers.append("CAPTCHA")
    assert "CAPTCHA" not in [pf.field.label for pf in review.blocking_fields(packet)]


def test_an_owner_answer_replaces_the_blocked_one(store):
    packet = review.apply_answers(draft(), {"question_7": "No"}, AnswerBank(store))
    contact = packet.answers[2]
    assert (contact.value, contact.source, contact.confident) == ("No", "owner", True)


def test_an_owner_answer_clears_its_blocker(store):
    packet = review.apply_answers(draft(), {"question_7": "No"}, AnswerBank(store))
    assert packet.blockers == ["could not fill: Education: Degree"]


def test_owner_answers_go_into_the_answer_bank(store):
    bank = AnswerBank(store)
    review.apply_answers(draft(), {"degree--0": "Bachelor's Degree"}, bank)
    assert bank.lookup("Education: Degree") == "Bachelor's Degree"


def test_skipped_questions_stay_blocking(store):
    packet = review.apply_answers(draft(), {}, AnswerBank(store))
    assert packet.blockers == draft().blockers


def test_an_owner_answer_must_be_one_of_the_known_options(store):
    with pytest.raises(ValueError, match="Yes, No"):
        review.apply_answers(draft(), {"question_7": "Maybe"}, AnswerBank(store))


# --- aja review -------------------------------------------------------------------------------

runner = CliRunner()
LYFT = Job(
    url="https://job-boards.greenhouse.io/embed/job_app?for=lyft&token=8896679002",
    company="Lyft",
    role="Software Engineer",
    ats=ATS.GREENHOUSE,
)


class ReviewSession:
    def __init__(self):
        self.filled = []
        self.highlighted = []

    def fill(self, plan):
        self.filled += plan
        return []

    def highlight(self, dom_ids):
        self.highlighted += dom_ids


@pytest.fixture
def staged_lyft(aja_home, monkeypatch):
    s = JobStore(aja_home / "jobs.db")
    s.upsert_many([LYFT])
    job_id = s.list()[0].id
    s.save_packet(job_id, draft())
    s.set_status(job_id, Status.STAGED, "needs review: 2 blocking")
    s.close()
    session = ReviewSession()

    @contextmanager
    def fake_session(job):
        yield session

    monkeypatch.setattr(cli, "_greenhouse_session", fake_session)
    return job_id, session


def reopen(aja_home):
    return JobStore(aja_home / "jobs.db")


def test_review_lists_drafts_and_manual_jobs(aja_home, staged_lyft):
    s = reopen(aja_home)
    s.upsert_many([Job(url="https://jobs.ashbyhq.com/acme/1", company="Acme", role="SWE")])
    acme = next(j for j in s.list() if j.company == "Acme")
    s.set_status(acme.id, Status.MANUAL, "unsupported site: apply manually")
    s.close()
    result = runner.invoke(cli.app, ["review"])
    assert result.exit_code == 0, result.output
    assert "Lyft" in result.output and "needs review: 2 blocking" in result.output
    assert "https://jobs.ashbyhq.com/acme/1" in result.output


def test_review_asks_the_blockers_refills_and_records_the_submit(aja_home, staged_lyft):
    job_id, session = staged_lyft
    typed = "Bachelor's Degree\nno\ny\n"
    result = runner.invoke(cli.app, ["review", str(job_id)], input=typed)
    assert result.exit_code == 0, result.output
    assert ("degree--0", "Bachelor's Degree") in {(a.dom_id, a.value) for a in session.filled}
    assert ("question_7", "No") in {(a.dom_id, a.value) for a in session.filled}
    s = reopen(aja_home)
    assert s.get(job_id).status is Status.APPLIED
    assert AnswerBank(s).lookup("Education: Degree") == "Bachelor's Degree"
    s.close()


def test_review_highlights_what_is_still_blank(aja_home, staged_lyft):
    job_id, session = staged_lyft
    runner.invoke(cli.app, ["review", str(job_id)], input="\n\nn\n")
    assert session.highlighted == ["degree--0", "question_7"]


def test_review_without_a_submit_keeps_the_draft(aja_home, staged_lyft):
    job_id, _ = staged_lyft
    runner.invoke(cli.app, ["review", str(job_id)], input="\n\nn\n")
    s = reopen(aja_home)
    assert s.get(job_id).status is Status.STAGED
    s.close()


def test_review_asks_again_when_the_answer_is_not_an_option(aja_home, staged_lyft):
    job_id, session = staged_lyft
    result = runner.invoke(cli.app, ["review", str(job_id)], input="\nmaybe\nNo\nn\n")
    assert "choose one of Yes, No" in result.output
    assert ("question_7", "No") in {(a.dom_id, a.value) for a in session.filled}


def test_review_of_a_job_without_a_draft_fails(aja_home):
    result = runner.invoke(cli.app, ["review", "42"])
    assert result.exit_code == 1
    assert "No draft for #42" in result.output


def test_a_work_authorization_answer_points_to_the_profile_not_the_bank(aja_home, monkeypatch):
    auth = FormField(
        "Work Authorization", "select", True, ["I am authorized", "I need sponsorship"]
    )
    packet = Packet(
        [PageField("question_9", auth)],
        [ResolvedAnswer(auth, None, "none", False)],
        ["Work Authorization"],
    )
    s = JobStore(aja_home / "jobs.db")
    s.upsert_many([LYFT])
    job_id = s.list()[0].id
    s.save_packet(job_id, packet)
    s.close()

    @contextmanager
    def fake_session(job):
        yield ReviewSession()

    monkeypatch.setattr(cli, "_greenhouse_session", fake_session)
    result = runner.invoke(cli.app, ["review", str(job_id)], input="I am authorized\nn\n")
    assert "approved_answers" in result.output
    assert '"I am authorized"' in result.output
