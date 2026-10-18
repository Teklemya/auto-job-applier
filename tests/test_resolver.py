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


def test_optional_salary_stays_blank_even_with_a_banked_answer(profile, bank):
    bank.save("Salary expectations", "150000")
    [a] = run([F("Salary expectations", required=False)], profile, bank)
    assert (a.value, a.source, a.confident) == (None, "preferences", True)


# --- labels as the Greenhouse engine reads them (T7) ---


def test_greenhouse_city_location_gets_city_and_state(profile, bank):
    [a] = run([F("Location (City)", kind="select")], profile, bank)
    assert (a.value, a.source) == ("Kansas City, MO", "profile")


def test_greenhouse_phone_country_dropdown_gets_the_profile_country(profile, bank):
    [a] = run([F("Country", kind="select")], profile, bank)
    assert a.value == "United States"


def test_employment_section_company_and_title_come_from_the_current_job(profile, bank):
    company, title = run([F("Employment: Company name"), F("Employment: Title")], profile, bank)
    assert (company.value, title.value) == ("Example Corp", "Software Engineer")


def test_employment_start_date_is_never_the_preferred_start_date(profile, bank):
    model = FakeLLM(llm_says((0, "June", False)))
    [a] = run([F("Employment: Start date month", kind="select")], profile, bank, model)
    assert a.source != "preferences"
    assert a.value != "Two weeks after offer"


def test_education_section_dropdowns_without_options_get_the_profile_value(profile, bank):
    school, degree, discipline = run(
        [
            F("Education: School", kind="select"),
            F("Education: Degree", kind="select"),
            F("Education: Discipline", kind="select"),
        ],
        profile,
        bank,
    )
    assert school.value == "Example University"
    assert degree.value == "Bachelor of Science"
    assert discipline.value == "Computer Science"


def test_military_service_is_not_an_export_control_question(profile, bank):
    # "itar" (export control) used to match inside "military" and block the field for good.
    label = "Please enter your relevant employment and military service above."
    model = FakeLLM(llm_says((0, "Thank you", True)))
    [a] = run([F(label, kind="select", options=["Thank you"])], profile, bank, model)
    assert (a.value, a.source) == ("Thank you", "llm")


def test_review_answer_beats_the_profile_for_dropdowns_with_unknown_options(profile, bank):
    # Greenhouse's degree list says "Bachelor's Degree", the profile "Bachelor of Science".
    # The owner picks the option once at review; that answer must win from then on.
    bank.save("Education: Degree", "Bachelor's Degree")
    [a] = run([F("Education: Degree", kind="select")], profile, bank)
    assert (a.value, a.source) == ("Bachelor's Degree", "bank")


def test_profile_still_beats_the_bank_when_the_options_are_known(profile, bank):
    bank.save("Gender", "Female")
    options = ["Decline To Self Identify", "Female", "Male"]
    [a] = run([F("Gender", kind="select", options=options)], profile, bank)
    assert a.source == "profile"


def test_employment_current_role_is_ticked_for_the_profiles_current_job(profile, bank):
    [a] = run([F("Employment: Current role", kind="checkbox", required=False)], profile, bank)
    assert (a.value, a.source) == ("Yes", "profile")


# --- work authorization: option sentences the owner approved in the profile (option A) ---

SEATGEEK_AUTH = [
    "Yes, I have US work authorization as a US Citizen or US Permanent Resident/Green Card Holder",
    "Yes, I have US Work authorization via a non-immigrant visa (H-1B, L-1, F-1 OPT OR CPT, TN)",
    "No, I am not currently authorized to work in the United States",
]
LYFT_AUTH = [
    "I am authorized to work for any employer in the country in which this position is based.",
    "I require/will require Lyft's sponsorship to obtain work authorization (e.g. H-1B, TN, etc.)",
    "My status to work in the country in which this position is based is unknown.",
]


def approving(example_profile_data, write_profile, *sentences):
    example_profile_data["work_authorization"]["approved_answers"] = list(sentences)
    return load_profile(write_profile(example_profile_data))


def test_an_approved_option_sentence_answers_a_work_authorization_question(
    example_profile_data, write_profile, bank
):
    profile = approving(example_profile_data, write_profile, SEATGEEK_AUTH[1])
    field = F(
        "Are you currently authorized to work in the United States?", "select", True, SEATGEEK_AUTH
    )
    [a] = run([field], profile, bank)
    assert (a.value, a.source, a.confident) == (SEATGEEK_AUTH[1], "profile", True)


def test_approved_sentences_match_despite_case_and_punctuation(
    example_profile_data, write_profile, bank
):
    approved = (
        "i am authorized to work for any employer in the country in which this position is based"
    )
    profile = approving(example_profile_data, write_profile, approved)
    [a] = run([F("Work Authorization", "select", True, LYFT_AUTH)], profile, bank)
    assert a.value == LYFT_AUTH[0]


def test_without_an_approved_sentence_the_long_options_still_block(profile, bank):
    [a] = run([F("Work Authorization", "select", True, LYFT_AUTH)], profile, bank)
    assert a.value is None and not a.confident


def test_two_approved_sentences_on_one_question_block_rather_than_pick(
    example_profile_data, write_profile, bank
):
    profile = approving(example_profile_data, write_profile, *SEATGEEK_AUTH[:2])
    field = F(
        "Are you currently authorized to work in the United States?", "select", True, SEATGEEK_AUTH
    )
    [a] = run([field], profile, bank)
    assert a.value is None and not a.confident


def test_approved_sentences_never_answer_other_questions(example_profile_data, write_profile, bank):
    profile = approving(example_profile_data, write_profile, "Yes")
    model = FakeLLM(llm_says((0, "No", True)))
    [a] = run([F("Do you enjoy hiking?", "select", True, YES_NO)], profile, bank, model)
    assert a.source == "llm"


# --- work authorization: the owner's own explanation for open-ended text questions ---

EXPLANATION = "I am authorized to work in the US on F-1 OPT and in the green card process."


def explaining(example_profile_data, write_profile):
    example_profile_data["work_authorization"]["explanation"] = EXPLANATION
    return load_profile(write_profile(example_profile_data))


@pytest.mark.parametrize(
    "label",
    [
        "If you're not authorized to work at the stated location, what sponsorship would you "
        "require for the role?",
        "Please describe your current work authorization status.",
        "Explain your visa sponsorship needs, if any.",
    ],
)
def test_open_ended_work_authorization_text_gets_the_owners_explanation(
    example_profile_data, write_profile, bank, label
):
    profile = explaining(example_profile_data, write_profile)
    [a] = run([F(label, "textarea", required=False)], profile, bank)
    assert (a.value, a.source, a.confident) == (EXPLANATION, "profile", True)


def test_yes_no_sponsorship_question_in_a_text_box_still_gets_no(
    example_profile_data, write_profile, bank
):
    profile = explaining(example_profile_data, write_profile)
    [a] = run([F("Do you require visa sponsorship?")], profile, bank)
    assert a.value == "No"


def test_visa_status_question_still_gets_the_visa_status(example_profile_data, write_profile, bank):
    profile = explaining(example_profile_data, write_profile)
    [a] = run([F("What is your current visa status?")], profile, bank)
    assert a.value == "F-1 OPT"


@pytest.mark.parametrize(
    "label",
    ["Are you a U.S. citizen?", "What is your citizenship?", "Describe your U.S. person status"],
)
def test_citizenship_questions_never_get_the_explanation(
    example_profile_data, write_profile, bank, label
):
    profile = explaining(example_profile_data, write_profile)
    [a] = run([F(label)], profile, bank)
    assert a.value != EXPLANATION
    assert not a.confident


def test_dropdowns_never_get_the_explanation(example_profile_data, write_profile, bank):
    profile = explaining(example_profile_data, write_profile)
    [a] = run([F("Work Authorization", "select", True, LYFT_AUTH)], profile, bank)
    assert a.value is None and not a.confident


def test_without_an_explanation_open_ended_questions_keep_todays_answer(profile, bank):
    label = "Please describe your current work authorization status."
    [a] = run([F(label, "textarea")], profile, bank)
    assert a.value == "Yes"
