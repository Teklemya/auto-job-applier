import pytest

from auto_job_applier.answers.bank import AnswerBank, normalize
from auto_job_applier.store import JobStore


@pytest.fixture
def bank(tmp_path):
    s = JobStore(tmp_path / "jobs.db")
    yield AnswerBank(s)
    s.close()


def test_normalize_ignores_case_punctuation_and_required_marks():
    assert normalize("  Are you 18+ years old? * ") == "are you 18 years old"


def test_lookup_exact_and_fuzzy(bank):
    bank.save("Are you at least 18 years of age?", "Yes")
    assert bank.lookup("are you at least 18 years of age") == "Yes"
    assert bank.lookup("Are you at least 18 years of age? (required)") == "Yes"
    assert bank.lookup("Are you at least 21 years of age?") is None  # different question
    assert bank.lookup("Do you have a driver's license?") is None


def test_save_overwrites_the_same_question(bank):
    bank.save("Preferred pronouns?", "he/him")
    bank.save("preferred pronouns", "they/them")
    assert bank.lookup("Preferred pronouns?") == "they/them"


def test_bank_persists_across_store_instances(tmp_path):
    s = JobStore(tmp_path / "jobs.db")
    AnswerBank(s).save("Q?", "A")
    s.close()
    s2 = JobStore(tmp_path / "jobs.db")
    assert AnswerBank(s2).lookup("Q?") == "A"
    s2.close()
