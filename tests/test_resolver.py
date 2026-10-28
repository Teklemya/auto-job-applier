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
        F("Are you willing to work weekends?", "select", options=YES_NO),
    ]
    model = FakeLLM(llm_says((1, "Yes", True), (2, "No", False)))
    got = run(fields, profile, bank, model)
    [call] = model.calls
    assert "Python professionally" in call["prompt"] and "weekends" in call["prompt"]
    assert "First Name" not in call["prompt"]
    assert call["attachments"] == [(profile.resume_path.read_bytes(), "application/pdf")]
    assert [(a.value, a.source, a.confident) for a in got[1:]] == [
        ("Yes", "llm", True),
        (None, "llm", False),  # never filled in when unsure (D1)
    ]
    assert "'No'" in got[2].reason  # the suggestion is kept for review
    assert resolver.gate(got) == ["Are you willing to work weekends?"]


def test_llm_value_outside_options_is_rejected(profile, bank):
    model = FakeLLM(llm_says((0, "Sure", True)))
    [a] = run([F("Comfortable with travel?", "select", options=YES_NO)], profile, bank, model)
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
    profile.education[0].degree_also = []
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


def test_a_non_hyphen_prefix_is_not_a_negation():
    # "non-immigrant" is not a "no": the F-1 option must stay reachable, and a bare "Yes"
    # fits both "Yes" options, so it must not pick the citizen one.
    field = F(
        "Are you currently authorized to work in the United States?", "select", True, SEATGEEK_AUTH
    )
    assert resolver.match_option("F-1 OPT", field) == SEATGEEK_AUTH[1]
    assert resolver.match_option("Yes", field) is None


def test_degree_spellings_count_as_the_same_option(profile, bank):
    profile.education[0].degree_also = []
    field = F("Degree", "select", options=["High School", "Bachelor's Degree", "Master's Degree"])
    profile.education[0].degree = "Bachelors"
    [a] = run([field], profile, bank)
    assert (a.value, a.source) == ("Bachelor's Degree", "profile")


def test_the_first_listed_choice_the_form_offers_wins(profile, bank):
    profile.education[0].major = "Information Technology"
    profile.education[0].major_also = ["Computer Information Systems", "Computer Science"]
    field = F("Discipline", "select", options=["Computer Science", "Computer Information Systems"])
    [a] = run([field], profile, bank)
    assert a.value == "Computer Information Systems"


def test_a_type_to_search_dropdown_gets_every_choice_to_try_in_order(profile, bank):
    profile.education[0].major = "Information Technology"
    profile.education[0].major_also = ["Computer Science"]
    [a] = run([F("Education: Discipline", "select", required=True)], profile, bank)
    assert (a.value, a.alternatives, a.confident) == (
        "Information Technology",
        ["Computer Science"],
        True,
    )


def test_a_review_answer_is_tried_before_the_profile_choices(profile, bank):
    profile.education[0].major = "Information Technology"
    profile.education[0].major_also = ["Computer Science"]
    bank.save("Education: Discipline", "Computer Science")
    [a] = run([F("Education: Discipline", "select", required=True)], profile, bank)
    assert (a.value, a.source, a.alternatives) == (
        "Computer Science",
        "bank",
        ["Information Technology"],
    )


def test_how_did_you_first_learn_about_us_is_a_preference(profile, bank):
    [a] = run(
        [F("How did you first learn about Affirm as an employer?", required=True)], profile, bank
    )
    assert (a.value, a.source) == ("Job board", "preferences")


def test_heard_about_us_takes_the_first_choice_the_form_offers(profile, bank):
    field = F("How did you hear about us?", "select", options=["LinkedIn", "Referral", "Other"])
    [a] = run([field], profile, bank)
    assert a.value == "Other"


def test_pronouns_come_from_the_profile_not_the_gender_answer(profile, bank):
    profile.demographics.pronouns = "He/Him"
    field = F("Pronouns", "select", required=True, options=["She/Her", "He/Him", "They/Them"])
    [a] = run([field], profile, bank)
    assert (a.value, a.source) == ("He/Him", "profile")


def test_no_pronouns_in_the_profile_leaves_the_question_for_review(profile, bank):
    [a] = run([F("Please share your gender pronouns.", required=False)], profile, bank)
    assert a.value is None


def test_education_dates_come_from_start_and_graduation(profile, bank):
    profile.education[0].start = "2020-08"
    profile.education[0].graduation = "2024-05"
    months = ["January", "May", "August"]
    fields = [
        F("Education: Start date month", "select", required=True, options=months),
        F("Education: Start date year", required=True),
        F("Education: End date month", "select", required=True),
        F("Education: End date year", required=True),
    ]
    start_month, start_year, end_month, end_year = run(fields, profile, bank)
    assert (start_month.value, start_year.value, end_year.value) == ("August", "2020", "2024")
    assert (end_month.value, end_month.alternatives) == ("May", ["05", "5"])


AFFIRM_STYLE = [
    "I have not previously been employed at Acme",
    "I have been employed at Acme as a full-time employee",
    "I have been employed at Acme as a contractor or intern",
]


def test_worked_here_before_picks_the_negated_sentence_option(profile, bank):
    q = F(
        "Have you previously been employed at Acme for any length of time?",
        "select",
        options=AFFIRM_STYLE,
    )
    [a] = run([q], profile, bank)
    assert (a.value, a.source) == ("I have not previously been employed at Acme", "profile")


def test_worked_for_us_before_is_no_on_a_yes_no_question(profile, bank):
    [a] = run([F("Have you ever worked for us before?", "select", options=YES_NO)], profile, bank)
    assert a.value == "No"


def test_a_past_employer_answers_yes(profile, bank):
    profile.experience.past_employers = ["ACME"]
    q = F("Have you ever been employed by Acme?", "select", options=YES_NO)
    [a] = run([q], profile, bank)
    assert a.value == "Yes"


def test_several_yes_options_are_left_for_review(profile, bank):
    profile.experience.past_employers = ["Acme"]
    q = F("Have you previously been employed at Acme?", "select", options=AFFIRM_STYLE)
    [a] = run([q], profile, bank, model=FakeLLM(llm_says((0, "x", False))))
    assert not a.confident


def test_worked_elsewhere_questions_are_not_about_this_company(profile, bank):
    q = F("Have you previously worked in a startup?", "select", options=YES_NO)
    [a] = run([q], profile, bank, model=FakeLLM(llm_says((0, "Yes", True))))
    assert a.source == "llm"


def test_relatives_at_the_company_is_no_unless_listed(profile, bank):
    q = F(
        "Are any of your immediate family members employees or directors of Acme or Acme "
        "Health PLLC, including a spouse or partner living in same household?",
        "select",
        options=YES_NO,
    )
    [a] = run([q], profile, bank)
    assert (a.value, a.source) == ("No", "profile")
    profile.experience.family_employed_at = ["Acme"]
    [a] = run([q], profile, bank)
    assert a.value == "Yes"


def test_relatives_question_as_free_text_says_no(profile, bank):
    [a] = run([F("Do you have any relatives who work at Acme?")], profile, bank)
    assert a.value == "No"


@pytest.mark.parametrize(
    "question",
    [
        "Are you located in the US?",
        "Do you currently reside in the United States?",
        "Are you based in the U.S.?",
    ],
)
def test_living_in_the_us_comes_from_the_address(profile, bank, question):
    [a] = run([F(question, "select", options=YES_NO)], profile, bank)
    assert (a.value, a.source) == ("Yes", "profile")


@pytest.mark.parametrize(
    "question",
    [
        "Have you ever worked for a competitor of Acme?",
        "Have you previously applied to work at Acme?",
        "Have you previously worked in the US?",
        "Have you ever worked with us as a vendor or client?",
    ],
)
def test_near_miss_employment_questions_are_not_guessed(profile, bank, question):
    q = F(question, "select", options=YES_NO)
    [a] = run([q], profile, bank, model=FakeLLM(llm_says((0, "Yes", False))))
    assert a.source != "profile"


def test_where_in_the_us_wants_a_place_not_yes(profile, bank):
    [a] = run([F("Where are you currently located in the US?")], profile, bank)
    assert a.value == f"{profile.address.city}, {profile.address.state}"


def test_dotted_country_counts_as_the_us(profile, bank):
    profile.address.country = "U.S.A."
    [a] = run([F("Are you located in the US?", "select", options=YES_NO)], profile, bank)
    assert a.value == "Yes"


def test_an_empty_heard_about_list_is_left_to_other_sources(profile, bank):
    profile.preferences.heard_about_us = []
    q = F("How did you hear about us?", "select", options=[])
    [a] = run([q], profile, bank, model=FakeLLM(llm_says((0, "x", False))))
    assert not a.confident


def test_a_bare_graduation_year_answers_the_year_but_not_the_month(profile, bank):
    profile.education[0].graduation = "2024"
    year, month = run(
        [F("Education: End date year"), F("Education: End date month")],
        profile,
        bank,
        model=FakeLLM(llm_says((1, "May", False))),
    )
    assert year.value == "2024"
    assert not month.confident


START_WINDOW = (
    "The start date for this role falls in 2027. Are you able to start between January 2027 "
    "and September 2027?"
)
HYBRID = (
    "Our internship roles may be remote or hybrid depending on team and location. Hybrid roles "
    "are based in San Francisco, Palo Alto, or Seattle and require some in-office attendance for "
    "the duration of the internship, based on team needs. Would you be able to meet this "
    "requirement if matched to a hybrid role?"
)
CONSENT = (
    "By checking this box, I consent to Acme collecting, storing, and processing my responses "
    "to the demographic data surveys above."
)


def test_a_start_window_is_yes_when_the_earliest_start_falls_inside_it(profile, bank):
    q = F(START_WINDOW, "select", options=YES_NO)
    profile.preferences.earliest_start = "2027-01"
    [a] = run([q], profile, bank)
    assert (a.value, a.source) == ("Yes", "preferences")
    profile.preferences.earliest_start = "2027-11"
    [a] = run([q], profile, bank)
    assert a.value == "No"


def test_a_start_window_without_an_earliest_start_goes_to_review(profile, bank):
    q = F(START_WINDOW, "select", options=YES_NO)
    profile.preferences.earliest_start = None
    [a] = run([q], profile, bank, model=FakeLLM(llm_says((0, "Yes", False))))
    assert not a.confident


def test_hybrid_requirement_comes_from_the_preference(profile, bank):
    q = F(HYBRID, "select", options=YES_NO)
    profile.preferences.in_office_ok = True
    [a] = run([q], profile, bank)
    assert (a.value, a.source) == ("Yes", "preferences")


def test_engineering_focus_and_office_take_the_first_choice_offered(profile, bank):
    profile.preferences.engineering_focus = ["Software Engineering", "Backend"]
    profile.preferences.office_locations = ["Seattle", "San Francisco"]
    focus = F(
        "What type of engineering are you primarily interested in?",
        "select",
        options=["Mobile", "Security", "Backend", "Full Stack", "Web"],
    )
    office = F(
        "What is your preferred office location?",
        "select",
        options=["San Francisco", "Palo Alto", "Seattle", "Remote"],
    )
    a, b = run([focus, office], profile, bank)
    assert (a.value, b.value) == ("Backend", "Seattle")


def test_demographic_consent_box_is_ticked_when_the_owner_agreed(profile, bank):
    q = F(CONSENT, "checkbox")
    profile.preferences.demographic_consent = True
    [a] = run([q], profile, bank)
    assert (a.value, a.confident) == ("Yes", True)


SANCTIONS = (
    "Please indicate whether you are either a citizen or resident of any of the following "
    "countries: Cuba, Iran, North Korea, Syria or Crimea Region of Ukraine."
)


def test_sanctioned_countries_question_comes_from_citizenships_and_address(profile, bank):
    q = F(SANCTIONS, "select", options=YES_NO)
    profile.work_authorization.citizenships = ["Ethiopia"]
    [a] = run([q], profile, bank)
    assert (a.value, a.source) == ("No", "profile")
    profile.work_authorization.citizenships = ["Iran"]
    [a] = run([q], profile, bank)
    assert a.value == "Yes"


def test_sanctioned_countries_question_without_citizenships_goes_to_review(profile, bank):
    profile.work_authorization.citizenships = []
    [a] = run([F(SANCTIONS, "select", options=YES_NO)], profile, bank)
    assert not a.confident


def test_sexual_orientation_takes_the_first_spelling_offered(profile, bank):
    q = F(
        "Voluntary Self-Identification of Sexual Orientation",
        "select",
        options=["Bisexual", "Heterosexual", "Homosexual", "I don't wish to answer"],
    )
    [a] = run([q], profile, bank)
    assert a.value == "I don't wish to answer"
    profile.demographics.sexual_orientation = ["Straight", "Heterosexual"]
    [a] = run([q], profile, bank)
    assert a.value == "Heterosexual"


def test_a_set_of_boxes_takes_the_first_heard_about_choice_offered(profile, bank):
    profile.preferences.heard_about_us = ["Job board", "Other"]
    q = F(
        "How did you hear about Twilio?",
        "checkboxes",
        options=["Careers Website", "LinkedIn", "Indeed", "Other"],
    )
    [a] = run([q], profile, bank)
    assert (a.value, a.source) == ("Other", "preferences")


def test_a_lone_acknowledge_box_is_ticked(profile, bank):
    q = F(
        'By clicking the "Acknowledge" button, you acknowledge that Acme processes data in '
        "accordance with the Applicant Privacy Policy.",
        "checkboxes",
        options=["Acknowledge"],
    )
    [a] = run([q], profile, bank)
    assert (a.value, a.confident) == ("Acknowledge", True)


def test_a_lone_box_that_states_a_fact_is_not_ticked(profile, bank):
    q = F("I certify that I am a U.S. citizen.", "checkboxes", options=["I agree"])
    [a] = run([q], profile, bank, model=FakeLLM(llm_says((0, "I agree", False))))
    assert not a.confident


AREAS = ["Frontend Engineer", "Backend Engineer", "Mobile Engineer", "Platform Engineer"]


@pytest.mark.parametrize(
    "which, expected", [("first", "Backend Engineer"), ("second", "Platform Engineer")]
)
def test_ranked_interest_questions_follow_the_engineering_focus_order(
    profile, bank, which, expected
):
    profile.preferences.engineering_focus = ["Software Engineering", "Backend", "Platform"]
    q = F(
        f"Out of the following four areas, please select your {which} choice of interest.",
        "select",
        options=AREAS,
    )
    [a] = run([q], profile, bank)
    assert (a.value, a.source) == (expected, "preferences")


def test_a_ranked_choice_past_the_list_goes_to_review(profile, bank):
    profile.preferences.engineering_focus = ["Backend"]
    q = F("Please select your second choice of interest.", "select", options=AREAS)
    [a] = run([q], profile, bank, model=FakeLLM(llm_says((0, "Mobile Engineer", False))))
    assert not a.confident


def test_a_graduation_year_the_form_does_not_list_is_other(profile, bank):
    profile.education[0].graduation = "2026-05"
    years = ["2027", "2028", "2029", "2030", "Other"]
    [a] = run([F("What is your expected graduation year?", "select", options=years)], profile, bank)
    assert a.value == "Other"
    [a] = run([F("Expected graduation year", "select", options=["2026", "Other"])], profile, bank)
    assert a.value == "2026"
    [a] = run([F("Graduation year")], profile, bank)
    assert a.value == "2026"


@pytest.mark.parametrize(
    "question, citizenships, expected",
    [
        (SANCTIONS, ["Ethiopia"], "No"),
        (
            "Are you a citizen or resident of Cuba, Iran, North Korea, Syria, or any country "
            "subject to comprehensive United States sanctions?",
            ["Ethiopia"],
            "No",
        ),
        (SANCTIONS, ["Ukraine"], None),  # a Crimea resident? the profile can't say
        (SANCTIONS, ["Syria"], "Yes"),
    ],
)
def test_sanctions_question_edge_cases(profile, bank, question, citizenships, expected):
    profile.work_authorization.citizenships = citizenships
    [a] = run([F(question, "select", options=YES_NO)], profile, bank)
    assert (a.value if a.confident else None) == expected


def test_site_reliability_is_not_an_on_site_question(profile, bank):
    profile.preferences.in_office_ok = True
    q = F("Do you have experience in site reliability engineering?", "select", options=YES_NO)
    [a] = run([q], profile, bank, model=FakeLLM(llm_says((0, "Yes", False))))
    assert a.source != "preferences"


def test_past_tense_start_is_not_a_start_window(profile, bank):
    profile.preferences.earliest_start = "2027-01"
    q = F("Did you start in 2020 at your current employer?", "select", options=YES_NO)
    [a] = run([q], profile, bank, model=FakeLLM(llm_says((0, "Yes", False))))
    assert a.source != "preferences"


@pytest.mark.parametrize(
    "earliest, expected", [("2026", "Yes"), ("2027", None), ("2028", "No"), ("2027-01", "Yes")]
)
def test_a_bare_year_earliest_start_decides_only_across_years(profile, bank, earliest, expected):
    profile.preferences.earliest_start = earliest
    q = F(START_WINDOW, "select", options=YES_NO)
    [a] = run([q], profile, bank, model=FakeLLM(llm_says((0, "Yes", False))))
    assert (a.value if a.confident else None) == expected


def test_a_lone_consent_box_follows_demographic_consent(profile, bank):
    q = F(
        "By checking this box, I consent to Acme processing my demographic survey responses.",
        "checkboxes",
        options=["I consent"],
    )
    profile.preferences.demographic_consent = False
    [a] = run([q], profile, bank, model=FakeLLM(llm_says((0, "I consent", False))))
    assert not a.confident


def test_non_compete_comes_from_the_profile(profile, bank):
    q = F(
        "Are you currently subject to any non-compete or non-solicitation agreement that would "
        "impact your ability to work at Acme or prevent you from accepting a job offer from Acme?",
        "select",
        options=YES_NO,
    )
    profile.experience.non_compete = False
    [a] = run([q], profile, bank)
    assert (a.value, a.source) == ("No", "profile")
    profile.experience.non_compete = None
    [a] = run([q], profile, bank, model=FakeLLM(llm_says((0, "No", False))))
    assert not a.confident


def test_transgender_is_its_own_question_not_gender(profile, bank):
    profile.demographics.gender = "Male"
    profile.demographics.transgender = "No"
    q = F("Do you identify as transgender?", "select", options=["Yes", "No", "Decline"])
    [a] = run([q], profile, bank)
    assert a.value == "No"


def test_ethnicities_plural_and_ranked_veteran_spellings(profile, bank):
    profile.demographics.race_ethnicity = "Black or African American"
    profile.demographics.veteran_status = ["I am not a protected veteran", "No military service"]
    ethnicity = F(
        "Please select up to 2 ethnicities that you most closely identify with.",
        "select",
        options=["American Indian or Alaska Native", "Black or African American", "White"],
    )
    veteran = F(
        "Protected Veteran Status",
        "select",
        options=["I am a veteran", "No military service", "I don't wish to answer"],
    )
    a, b = run([ethnicity, veteran], profile, bank)
    assert (a.value, b.value) == ("Black or African American", "No military service")


@pytest.mark.parametrize(
    "label",
    ["What is your current or previous job title?", "Most recent title"],
)
def test_current_or_previous_title_comes_from_the_profile(profile, bank, label):
    [a] = run([F(label)], profile, bank)
    assert (a.value, a.source) == ("Software Engineer", "profile")


def test_worked_here_with_an_apply_internally_note_is_still_answered(profile, bank):
    q = F(
        "Have you previously been employed by Acme or an Acme subsidiary (TrackAbout, AFT, "
        "etc.)? (If you are a current employee, please apply via the internal job board.)"
    )
    [a] = run([q], profile, bank)
    assert (a.value, a.source) == ("No", "profile")


def test_previously_applied_is_still_not_an_employment_question(profile, bank):
    q = F("Have you previously applied to work at Acme?")
    [a] = run([q], profile, bank, model=FakeLLM(llm_says((0, "No", True))))
    assert a.source == "llm"


def test_previous_job_title_alone_is_not_the_current_one(profile, bank):
    [a] = run([F("Previous job title")], profile, bank, model=FakeLLM(llm_says((0, "x", False))))
    assert a.source != "profile"


def test_non_compete_with_current_employer_is_yes_or_no(profile, bank):
    profile.experience.non_compete = False
    [a] = run(
        [
            F(
                "Are you subject to a non-compete with your current employer?",
                "select",
                options=["Yes", "No"],
            )
        ],
        profile,
        bank,
    )
    assert a.value == "No"


def test_a_note_alone_does_not_make_a_worked_here_question(profile, bank):
    q = F("Willing to relocate to work at Acme? (Former employees: apply internally.)")
    [a] = run([q], profile, bank, model=FakeLLM(llm_says((0, "Yes", True))))
    assert a.source != "profile"


GRAD_OPTIONS = [
    "I have already graduated",
    "December 2026 - November 2027",
    "I graduate after December 2027",
]


@pytest.mark.parametrize(
    "graduation, expected",
    [
        ("2026-05", "I have already graduated"),
        ("2027-05", "December 2026 - November 2027"),
        ("2026-12", "December 2026 - November 2027"),
        ("2028-05", "I graduate after December 2027"),
    ],
)
def test_graduation_date_picks_the_option_that_holds_it(profile, bank, graduation, expected):
    profile.education[0].graduation = graduation
    q = F("When is your anticipated graduation date?", "select", options=GRAD_OPTIONS)
    [a] = resolver.resolve(
        [q], profile, bank=bank, llm=FakeLLM(), job=JOB, jd=JD, today=lambda: "2026-10-28"
    )
    assert (a.value, a.source) == (expected, "profile")


def test_graduation_between_the_options_is_not_guessed(profile, bank):
    profile.education[0].graduation = "2027-06"
    q = F("Expected graduation?", "select", options=["Spring 2026", "May 2028"])
    [a] = resolver.resolve(
        [q],
        profile,
        bank=bank,
        llm=FakeLLM(llm_says((0, "May 2028", False))),
        job=JOB,
        jd=JD,
        today=lambda: "2026-10-28",
    )
    assert not a.confident


def test_archetype_comes_from_engineering_focus(profile, bank):
    profile.preferences.engineering_focus = ["Backend", "Full Stack"]
    q = F(
        "What archetype are you most closely aligned with?",
        "select",
        options=["Full Stack", "Big Data", "Machine Learning"],
    )
    [a] = run([q], profile, bank)
    assert (a.value, a.source) == ("Full Stack", "preferences")
