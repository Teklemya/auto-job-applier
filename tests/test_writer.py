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
