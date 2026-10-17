import pytest

from auto_job_applier import llm
from auto_job_applier.answers import writer
from auto_job_applier.models import FormField, Job
from auto_job_applier.profile import load_profile

JOB = Job(url="https://job-boards.greenhouse.io/acme/jobs/1", company="Acme", role="SWE I")
JD = "Acme builds payment APIs in Go and Python."
COVER = FormField("Cover Letter", "textarea", required=True)
WHY = FormField("Why do you want to work at Acme?", "textarea", required=True)


@pytest.fixture
def profile(example_profile_data, write_profile):
    return load_profile(write_profile(example_profile_data))


class FakeLLM:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def generate(self, **kw):
        self.calls.append(kw)
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a


def draft(words=120, confident=True, missing=None):
    return writer.Draft(
        text=" ".join(["word"] * words), confident=confident, missing_facts=missing or []
    )


def test_cover_letter_is_grounded_in_resume_and_jd(profile):
    model = FakeLLM(draft(200))
    a = writer.make_writer(profile, model)(COVER, JOB, JD)
    assert (a.source, a.confident) == ("llm", True)
    assert a.value.startswith("word")
    [call] = model.calls
    assert JD in call["prompt"] and "Acme" in call["prompt"] and "SWE I" in call["prompt"]
    assert "cover letter" in call["prompt"].lower()
    assert call["attachments"] == [(profile.resume_path.read_bytes(), "application/pdf")]
    assert call["schema"] is writer.Draft


def test_why_us_gets_a_shorter_limit(profile):
    model = FakeLLM(draft(80))
    writer.make_writer(profile, model)(WHY, JOB, JD)
    prompt = model.calls[0]["prompt"]
    assert f"at most {writer.SHORT_WORDS} words" in prompt
    assert WHY.label in prompt


def test_missing_facts_mean_not_confident(profile):
    model = FakeLLM(draft(confident=False, missing=["years of Go experience"]))
    a = writer.make_writer(profile, model)(COVER, JOB, JD)
    assert not a.confident and a.value is None
    assert "years of Go experience" in a.reason


def test_overlong_answer_is_rejected(profile):
    model = FakeLLM(draft(writer.SHORT_WORDS * 2))
    a = writer.make_writer(profile, model)(WHY, JOB, JD)
    assert not a.confident and "too long" in a.reason


def test_quota_exhausted_is_not_confident(profile):
    model = FakeLLM(llm.QuotaExhausted("daily LLM budget of 300 calls used"))
    a = writer.make_writer(profile, model)(COVER, JOB, JD)
    assert not a.confident and "budget" in a.reason


def _prompt(profile, field=COVER, today="2026-10-17"):
    model = FakeLLM(draft(150))
    writer.make_writer(profile, model, today=lambda: today)(field, JOB, JD)
    return model.calls[0]["prompt"]


def test_signs_with_preferred_name(profile):
    profile.contact.preferred_name = "Izzy"
    assert "sign off with 'Izzy Doe'" in _prompt(profile)


def test_signs_with_first_name_without_a_preferred_one(profile):
    profile.contact.preferred_name = None
    assert "sign off with 'Jane Doe'" in _prompt(profile)


def test_knows_today_and_whether_the_candidate_graduated(profile):
    # example profile: B.S. Computer Science, graduation 2024-05
    p = _prompt(profile, today="2026-10-17")
    assert "Today is 2026-10-17" in p
    assert "graduated 2024-05" in p
    assert "graduates 2024-05 (not yet graduated)" in _prompt(profile, today="2024-01-01")


def test_states_the_current_role_as_given(profile):
    assert "Current job: Software Engineer at Example Corp" in _prompt(profile)


def test_system_prompt_rules():
    s = " ".join(writer.SYSTEM.split())
    assert "I am applying" in s  # forbidden opener is named
    assert "exactly as the resume" in s  # titles and employment types not reworded
    assert "most relevant" in s  # pick the highlight per job
