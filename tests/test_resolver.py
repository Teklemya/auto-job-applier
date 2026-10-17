import pytest

from auto_job_applier import llm
from auto_job_applier.answers import resolver
from auto_job_applier.answers.bank import AnswerBank
from auto_job_applier.models import FormField, Job
from auto_job_applier.profile import load_profile
from auto_job_applier.store import JobStore

JOB = Job(url="https://job-boards.greenhouse.io/acme/jobs/1", company="Acme", role="SWE I")
JD = "Build backend services in Python."
YES_NO = ["Yes", "No"]


@pytest.fixture
def store(tmp_path):
    s = JobStore(tmp_path / "jobs.db")
    yield s
    s.close()


@pytest.fixture
def bank(store):
    return AnswerBank(store)


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


def llm_says(*items):
    """items: (index, value, confident)"""
    return resolver.FieldAnswers(
        answers=[resolver.FieldAnswer(id=i, value=v, confident=c, reason="r") for i, v, c in items]
    )


def run(fields, profile, bank, model=None, writer=None):
    return resolver.resolve(
        fields, profile, bank=bank, llm=model or FakeLLM(), job=JOB, jd=JD, writer=writer
    )


def F(label, kind="text", required=True, options=None):
    return FormField(label=label, kind=kind, required=required, options=options or [])


# --- profile ---


def test_contact_and_education_come_from_the_profile(profile, bank):
    fields = [
        F("First Name"),
        F("Last Name *"),
        F("Email"),
        F("Phone"),
        F("LinkedIn Profile", required=False),
        F("School"),
        F("Graduation Year"),
        F("Resume/CV", kind="file"),
    ]
    got = run(fields, profile, bank)
    assert [a.value for a in got] == [
        "Jane",
        "Doe",
        "jane.doe@example.com",
        "555-555-0100",
        "https://www.linkedin.com/in/janedoe",
        "Example University",
        "2024",
        str(profile.resume_path),
    ]
    assert {a.source for a in got} == {"profile"}
    assert all(a.confident for a in got)


@pytest.mark.parametrize(
    ("label", "options", "expected"),
    [
        (
            "Will you now or in the future require sponsorship for employment visa status?",
            YES_NO,
            "No",
        ),
        ("Are you legally authorized to work in the United States?", YES_NO, "Yes"),
        ("Are you eligible to work in the US?", ["Yes, I am", "No, I am not"], "Yes, I am"),
        ("Do you require visa sponsorship?", None, "No"),
        ("Are you authorized to work in the US without sponsorship?", YES_NO, "Yes"),
    ],
)
def test_work_authorization_comes_only_from_the_profile(profile, bank, label, options, expected):
    kind = "select" if options else "text"
    [a] = run([F(label, kind, options=options)], profile, bank, FakeLLM())
    assert (a.value, a.source, a.confident) == (expected, "profile", True)


@pytest.mark.parametrize(
    "label",
    [
        "Are you a U.S. citizen?",
        "What is your citizenship?",
        "Are you a U.S. person under ITAR export control regulations?",
        "Do you hold an H-1B visa?",
        "Do you have permanent residency (green card)?",
    ],
)
def test_unmapped_work_authorization_never_reaches_the_llm(profile, bank, label):
    model = FakeLLM()  # any call would pop from an empty list and fail
    [a] = run([F(label, "select", options=YES_NO)], profile, bank, model)
    assert model.calls == []
    assert a.value is None and not a.confident
    assert "work authorization" in a.reason
    assert resolver.gate([a]) == [label]


def test_work_authorization_option_mismatch_blocks_instead_of_asking_llm(profile, bank):
    model = FakeLLM()
    field = F("Do you require sponsorship?", "select", options=["H-1B", "TN", "None of these"])
    [a] = run([field], profile, bank, model)
    assert model.calls == [] and not a.confident


def test_bank_never_answers_work_authorization(profile, bank):
    bank.save("Are you a U.S. citizen?", "Yes")
    [a] = run([F("Are you a U.S. citizen?", "select", options=YES_NO)], profile, bank)
    assert a.value is None


def test_eeo_decline_maps_to_the_forms_decline_option(profile, bank):
    field = F(
        "Gender",
        "select",
        required=False,
        options=["Male", "Female", "Non-binary", "I don't wish to answer"],
    )
    [a] = run([field], profile, bank)
    assert (a.value, a.source) == ("I don't wish to answer", "profile")


# --- preferences ---


def test_preferences(profile, bank):
    fields = [
        F("When can you start?"),
        F("How did you hear about us?", "select", options=["LinkedIn", "Job board", "Referral"]),
        F("Are you willing to relocate?", "select", options=YES_NO),
    ]
    got = run(fields, profile, bank)
    assert [(a.value, a.source) for a in got] == [
        ("Two weeks after offer", "preferences"),
        ("Job board", "preferences"),
        ("Yes", "preferences"),
    ]


def test_required_salary_uses_the_floor_and_optional_salary_stays_blank(profile, bank):
    req, opt = run([F("Desired salary"), F("Salary expectations", required=False)], profile, bank)
    assert (req.value, req.source, req.confident) == ("120000", "preferences", True)
    assert (opt.value, opt.confident) == (None, True)
    assert resolver.gate([req, opt]) == []


def test_unmatched_option_is_not_confident(profile, bank):
    field = F("How did you hear about us?", "select", options=["Billboard", "Podcast"])
    model = FakeLLM(llm_says((0, "Billboard", False)))
    [a] = run([field], profile, bank, model)
    assert not a.confident


# --- answer bank ---


def test_bank_answers_fuzzy_matched_questions(profile, bank):
    bank.save("Are you at least 18 years of age?", "Yes")
    [a] = run([F("Are you at least 18 years of age? *", "select", options=YES_NO)], profile, bank)
    assert (a.value, a.source, a.confident) == ("Yes", "bank", True)


def test_bank_answer_must_be_a_valid_option(profile, bank):
    bank.save("Are you at least 18 years of age?", "Yes")
    model = FakeLLM(llm_says((0, "Over 18", False)))
    field = F("Are you at least 18 years of age?", "select", options=["Over 18", "Under 18"])
    [a] = run([field], profile, bank, model)
    assert a.source == "llm"


# --- LLM ---


def test_remaining_fields_go_to_the_llm_in_one_call(profile, bank):
    fields = [
        F("First Name"),
        F("Have you used Python professionally?", "select", options=YES_NO),
        F("Do you have relatives working at Acme?", "select", options=YES_NO),
    ]
    model = FakeLLM(llm_says((1, "Yes", True), (2, "No", False)))
    got = run(fields, profile, bank, model)
    [call] = model.calls
    assert "Python professionally" in call["prompt"] and "relatives" in call["prompt"]
    assert "First Name" not in call["prompt"]
    assert call["attachments"] == [(profile.resume_path.read_bytes(), "application/pdf")]
    assert [(a.value, a.source, a.confident) for a in got[1:]] == [
        ("Yes", "llm", True),
        (None, "llm", False),  # never filled in when unsure (D1)
    ]
    assert "'No'" in got[2].reason  # the suggestion is kept for review
    assert resolver.gate(got) == ["Do you have relatives working at Acme?"]


def test_llm_value_outside_options_is_rejected(profile, bank):
    model = FakeLLM(llm_says((0, "Sure", True)))
    [a] = run([F("Comfortable on-site?", "select", options=YES_NO)], profile, bank, model)
    assert not a.confident


def test_llm_answer_with_missing_id_is_unresolved(profile, bank):
    model = FakeLLM(llm_says())
    [a] = run([F("Favorite language?")], profile, bank, model)
    assert a.value is None and not a.confident


def test_quota_exhausted_blocks_instead_of_guessing(profile, bank):
    model = FakeLLM(llm.QuotaExhausted("daily LLM budget of 300 calls used"))
    [a] = run([F("Favorite language?")], profile, bank, model)
    assert not a.confident and "budget" in a.reason
    assert resolver.gate([a]) == ["Favorite language?"]


def test_optional_unanswerable_fields_do_not_block(profile, bank):
    model = FakeLLM(llm_says((0, "", False)))
    [a] = run([F("Anything else?", "textarea", required=False)], profile, bank, model)
    assert resolver.gate([a]) == []


def test_no_llm_call_when_everything_resolves(profile, bank):
    model = FakeLLM()
    run([F("First Name"), F("Email")], profile, bank, model)
    assert model.calls == []


# --- long-form writing (T6 hook) ---


def test_optional_cover_letter_is_left_blank(profile, bank):
    model = FakeLLM()
    [a] = run([F("Cover Letter", "textarea", required=False)], profile, bank, model)
    assert (a.value, a.confident) == (None, True) and model.calls == []


def test_required_cover_letter_goes_to_the_writer(profile, bank):
    seen = []

    def writer(field, job, jd):
        seen.append(field.label)
        return resolver.ResolvedAnswer(field, "Dear team...", "llm", True, "written")

    [a] = run([F("Cover Letter", "file")], profile, bank, writer=writer)
    assert seen == ["Cover Letter"] and a.value == "Dear team..."


def test_required_cover_letter_without_writer_blocks(profile, bank):
    [a] = run([F("Cover Letter", "textarea")], profile, bank)
    assert not a.confident


def test_opt_in_is_not_a_visa_question(profile, bank):
    model = FakeLLM(llm_says((0, "No", True)))
    [a] = run(
        [F("Opt in to text message updates?", "select", options=YES_NO)], profile, bank, model
    )
    assert a.source == "llm"


@pytest.mark.parametrize(
    ("value", "options", "expected"),
    [
        # "no" must not match "Lati-no" or "Not ..." by substring
        ("No", ["Hispanic or Latino", "Not Hispanic or Latino"], None),
        ("No", ["Hispanic or Latino", "White"], None),  # substring would pick "Latino"
        ("No", ["Yes", "No", "Decline To Self Identify"], "No"),
        (
            "I am not a protected veteran",
            [
                "I identify as one or more of the classifications of protected veteran",
                "I am not a protected veteran",
                "I don't wish to answer",
            ],
            "I am not a protected veteran",
        ),
        (
            "I am not a protected veteran",
            ["I am a veteran", "I am not a veteran"],
            "I am not a veteran",
        ),
        (
            "No, I do not have a disability",
            [
                "Yes, I have a disability, or have had one in the past",
                "No, I do not have a disability and have not had one in the past",
                "I do not want to answer",
            ],
            "No, I do not have a disability and have not had one in the past",
        ),
        ("male", ["Male", "Female", "Non-binary"], "Male"),
        (
            "Black or African American",
            ["White", "Black or African American", "Asian"],
            "Black or African American",
        ),
    ],
)
def test_match_option_uses_whole_words(value, options, expected):
    assert resolver.match_option(value, F("q", "select", options=options)) == expected


@pytest.mark.parametrize(
    ("value", "options"),
    [
        # Dropping "not" must never flip the answer
        ("I am not a protected veteran", ["I am a veteran", "Decline to answer"]),
        ("No, I do not have a disability", ["I have a disability", "Prefer not to say"]),
        # Placeholder options with no words match nothing
        ("Job board", ["--", "LinkedIn", "Referral"]),
        ("Job board", ["", "LinkedIn"]),
    ],
)
def test_match_option_never_flips_negation_or_picks_placeholders(value, options):
    assert resolver.match_option(value, F("q", "select", options=options)) is None


def test_match_option_still_finds_the_option_next_to_a_placeholder():
    field = F("q", "select", options=["--", "Job board", "LinkedIn"])
    assert resolver.match_option("Job board", field) == "Job board"


def test_optional_fields_never_reach_the_llm(profile, bank):
    model = FakeLLM()
    [a] = run([F("Favorite color?", required=False)], profile, bank, model)
    assert model.calls == [] and a.value is None and a.confident


def test_profile_value_that_fits_no_option_falls_through(profile, bank):
    bank.save("Highest degree", "Bachelor's")
    field = F("Highest degree", "select", options=["High school", "Bachelor's", "Master's"])
    [a] = run([field], profile, bank)  # profile says "Bachelor of Science": no option fits
    assert (a.value, a.source) == ("Bachelor's", "bank")


def test_gate_blocks_required_field_without_a_value_even_if_marked_confident():
    field = F("Anything")
    assert resolver.gate([resolver.ResolvedAnswer(field, None, "llm", True)]) == ["Anything"]


def test_llm_facts_use_the_preferred_name_and_graduation_status(profile, bank):
    profile.contact.preferred_name = "Izzy"
    model = FakeLLM(llm_says((0, "x", True)))
    resolver.resolve(
        [F("Favorite language?")],
        profile,
        bank=bank,
        llm=model,
        job=JOB,
        jd=JD,
        today=lambda: "2026-10-17",
    )
    prompt = model.calls[0]["prompt"]
    assert "Izzy Doe" in prompt and "graduated 2024-05" in prompt
