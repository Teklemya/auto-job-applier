import json

import pytest
from conftest import FIXTURES

from auto_job_applier.engines import greenhouse
from auto_job_applier.engines.base import Action

GH = FIXTURES / "greenhouse"


def form(company):
    """A real Greenhouse application form as rendered in Chrome, plus its public questions API."""
    html = (GH / f"{company}.html").read_text()
    api = json.loads((GH / f"{company}_api.json").read_text())
    return greenhouse.extract_fields(html, api)


def by_id(fields, dom_id):
    return next(f for f in fields if f.dom_id == dom_id)


def labels(fields):
    return [f.field.label for f in fields]


# --- what counts as a field -------------------------------------------------------------------


def test_contact_fields_are_read_with_their_labels():
    fields = form("lyft")
    assert labels(fields)[:3] == ["First Name", "Last Name", "Email"]
    assert by_id(fields, "first_name").field.kind == "text"


def test_required_comes_from_aria_required():
    fields = form("brex")
    assert by_id(fields, "first_name").field.required
    assert not by_id(fields, "phone").field.required  # Brex makes phone optional


def test_dropdowns_are_select_fields():
    fields = form("lyft")
    contact = by_id(fields, "question_38857527002").field
    assert contact.label == "May we contact your current employer?"
    assert contact.kind == "select"
    assert contact.required


def test_dropdown_options_come_from_the_questions_api():
    contact = by_id(form("lyft"), "question_38857527002").field
    assert contact.options == ["Yes", "No"]


def test_eeo_dropdown_options_come_from_the_compliance_section():
    gender = by_id(form("lyft"), "gender").field
    assert gender.options == ["Decline To Self Identify", "Female", "Male"]


def test_demographic_dropdown_options_come_from_the_demographic_section():
    race = by_id(form("seatgeek"), "43").field
    assert race.label == "Race"
    assert "Prefer not to say" in race.options
    assert not race.required


def test_dropdowns_the_api_doesnt_list_have_no_options_yet():
    # School, degree and the phone country load their options as you type.
    assert by_id(form("lyft"), "school--0").field.options == []


def test_resume_upload_is_a_required_file_field_labelled_by_its_group():
    resume = by_id(form("lyft"), "resume").field
    assert (resume.label, resume.kind, resume.required) == ("Resume/CV", "file", True)


def test_optional_cover_letter_upload_is_read_as_optional():
    cover = by_id(form("brex"), "cover_letter").field
    assert (cover.label, cover.kind, cover.required) == ("Cover Letter", "file", False)


def test_enter_manually_boxes_captcha_and_phone_country_search_are_not_fields():
    ids = {f.dom_id for f in form("lyft")}
    assert not ids & {"resume_text", "g-recaptcha-response-100000", "iti-0__search-input"}


def test_every_input_is_read_once():
    ids = [f.dom_id for f in form("lyft")]
    assert len(ids) == len(set(ids))


def test_every_api_question_shows_up_on_the_page():
    for company in ("lyft", "brex", "seatgeek"):
        api = json.loads((GH / f"{company}_api.json").read_text())
        on_page = {f.dom_id for f in form(company)}
        for q in api["questions"]:
            names = {f["name"] for f in q["fields"]} - {"resume_text", "cover_letter_text"}
            assert names <= on_page, (company, q["label"])


# --- sections ---------------------------------------------------------------------------------


def test_employment_fields_are_labelled_with_their_section():
    fields = form("lyft")
    assert by_id(fields, "company-name-0").field.label == "Employment: Company name"
    assert by_id(fields, "start-date-month-0").field.label == "Employment: Start date month"
    assert by_id(fields, "start-date-month-0").field.kind == "select"


def test_current_role_is_a_checkbox():
    current = by_id(form("lyft"), "current-role-0_1").field
    assert (current.label, current.kind) == ("Employment: Current role", "checkbox")


def test_education_fields_are_labelled_with_their_section():
    fields = form("lyft")
    assert by_id(fields, "degree--0").field.label == "Education: Degree"
    assert by_id(fields, "degree--0").field.required


def test_fields_after_a_section_are_not_labelled_with_it():
    assert by_id(form("lyft"), "gender").field.label == "Gender"


# --- choosing from a dropdown's live options -------------------------------------------------


def test_pick_option_prefers_an_exact_match():
    options = ["Male", "Female", "Decline To Self Identify"]
    assert greenhouse.pick_option("Female", options) == "Female"


def test_pick_option_ignores_case_and_spacing():
    assert greenhouse.pick_option("decline to self identify", ["Decline To  Self Identify"]) == (
        "Decline To  Self Identify"
    )


def test_pick_option_finds_a_place_by_its_city():
    options = ["Kansas City, Missouri, United States", "Kansas City, Kansas, United States"]
    assert greenhouse.pick_option("Kansas City, MO", options) == options[0]


def test_pick_option_uses_the_state_to_choose_between_same_named_cities():
    options = ["Kansas City, Kansas, United States", "Kansas City, Missouri, United States"]
    assert greenhouse.pick_option("Kansas City, MO", options) == options[1]


def test_pick_option_finds_a_school_by_its_name():
    options = ["University of Kansas", "Kansas State University"]
    assert greenhouse.pick_option("Kansas State University", options) == "Kansas State University"


def test_pick_option_returns_none_rather_than_guess():
    assert greenhouse.pick_option("Bachelor of Science", ["Master's Degree", "PhD"]) is None


def test_pick_option_returns_none_when_several_options_fit_equally():
    options = ["Springfield, Illinois, United States", "Springfield, Missouri, United States"]
    assert greenhouse.pick_option("Springfield", options) is None


# --- the questions API ------------------------------------------------------------------------


def test_questions_come_from_the_public_board_api():
    from auto_job_applier.models import ATS, Job

    job = Job(
        url="https://job-boards.greenhouse.io/embed/job_app?for=lyft&token=8896679002",
        company="Lyft",
        role="Software Engineer",
        ats=ATS.GREENHOUSE,
    )
    asked = []
    greenhouse.fetch_questions(job, get=lambda url: asked.append(url) or {"questions": []})
    assert asked == [
        "https://boards-api.greenhouse.io/v1/boards/lyft/jobs/8896679002?questions=true"
    ]


def test_fields_greyed_out_by_another_answer_are_not_fields():
    # Ticking "Current role" disables the end date (a CSS class, not a disabled attribute).
    html = (GH / "lyft_current_role.html").read_text()
    api = json.loads((GH / "lyft_api.json").read_text())
    ids = {f.dom_id for f in greenhouse.extract_fields(html, api)}
    assert "end-date-month-0" not in ids
    assert "end-date-year-0" not in ids
    assert "start-date-month-0" in ids


def test_submit_outcome_reads_the_confirmation_page():
    form = "https://job-boards.greenhouse.io/lyft/jobs/1"
    assert greenhouse.submit_outcome(f"{form}/confirmation", "", [], True).confirmed
    assert greenhouse.submit_outcome(form, "Thank you for applying to Lyft!", [], False).confirmed
    assert greenhouse.submit_outcome(
        form, "Your application has been submitted.", [], False
    ).confirmed


def test_submit_outcome_stages_a_security_code_or_page_errors():
    form = "https://job-boards.greenhouse.io/lyft/jobs/1"
    code = greenhouse.submit_outcome(form, "Enter the code", [], True, code_asked=True)
    assert not code.confirmed and "security code" in code.problems[0]
    rejected = greenhouse.submit_outcome(form, "Apply", ["Phone is invalid"], True)
    assert rejected.problems == ["page rejected the submit: Phone is invalid"]


def test_submit_outcome_waits_while_the_page_shows_nothing_new():
    assert greenhouse.submit_outcome("https://x/jobs/1", "Apply for this job", [], True) is None


def test_thank_you_text_in_the_job_description_is_not_a_confirmation():
    jd = "Once your application has been submitted, our team will reach out."
    assert greenhouse.submit_outcome("https://x/jobs/1", jd, [], form_showing=True) is None


def test_greenhouses_verification_code_page_is_recognized():
    text = (
        "A verification code was sent to j@x.com. To submit your application, enter the "
        "8-character code to confirm you're a human. Security code"
    )
    assert greenhouse._SECURITY_CODE.search(text)


def test_a_job_description_about_security_codes_does_not_hide_the_code_prompt():
    jd = "You will build security code review tools. Apply below."
    prompt = jd + " A verification code was sent to j@x.com. Security code"
    assert not greenhouse.code_newly_asked(jd, jd)
    assert greenhouse.code_newly_asked(jd, prompt)


MISSING_FORM = """
<form id="application-form">
  <div class="field-wrapper"><label for="first_name">First Name*</label>
    <input id="first_name" aria-required="true" value="Jane"></div>
  <div class="field-wrapper"><label for="phone">Phone*</label>
    <input id="phone" aria-required="true" value=""></div>
  <div class="field-wrapper"><label for="vet">Protected Veteran Status*</label>
    <div class="select-shell"><div class="select__placeholder">Select...</div>
    <input id="vet" role="combobox" aria-required="true">
    <input required tabindex="-1" aria-hidden="true" value=""></div></div>
  <div class="field-wrapper"><label for="gender">Gender*</label>
    <div class="select-shell"><div class="select__single-value">Male</div>
    <input id="gender" role="combobox" aria-required="true"></div></div>
  <div class="checkbox"><input type="checkbox" id="consent" required>
    <span>By checking this box, I consent to Twilio processing my responses *</span></div>
  <div class="checkbox"><label><input type="checkbox" id="ok" required checked> OK</label></div>
  <input id="optional_note" value="">
  <div style="display:none"><label for="hidden_q">Hidden*</label>
    <input id="hidden_q" aria-required="true"></div>
</form>
"""


def test_required_inputs_left_empty_are_found_on_the_page():
    sync_api = pytest.importorskip("playwright.sync_api")
    try:
        with sync_api.sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page()
            page.set_content(MISSING_FORM)
            found = [m[:2] for m in greenhouse.GreenhouseSession(page, None).missing_required()]
            browser.close()
    except sync_api.Error as e:  # no browser installed here
        pytest.skip(str(e))
    assert found == [
        ("phone", "Phone"),
        ("vet", "Protected Veteran Status"),
        ("consent", "By checking this box, I consent to Twilio processing my responses"),
    ]


def _box(group, n, text):
    return (
        f'<div class="checkbox__wrapper"><div class="checkbox__input">'
        f'<input type="checkbox" id="{group}_{n}" name="{group}" value="{n}"></div>'
        f'<label for="{group}_{n}">{text}</label></div>'
    )


# As Twilio's form shows them: a fieldset per question, the question in its legend.
HEARD = "question_65614028[]"
ACK = "question_65614029[]"
BOX_GROUPS = (
    '<form id="application-form"><div class="field-wrapper">'
    f'<fieldset class="checkbox" id="{HEARD}" aria-required="true">'
    '<legend class="label checkbox__description">How did you hear about Twilio?'
    '<span aria-hidden="true">*</span></legend>'
    + "".join(_box(HEARD, n, t) for n, t in enumerate(["Careers Website", "LinkedIn", "Other"]))
    + '</fieldset></div><div class="field-wrapper">'
    f'<fieldset class="checkbox" id="{ACK}" aria-required="true">'
    '<legend class="label checkbox__description">By clicking the "Acknowledge" button, you '
    "acknowledge that Twilio processes data in accordance with the Applicant Privacy Policy."
    "</legend>" + _box(ACK, 0, "Acknowledge") + "</fieldset></div></form>"
)


def test_a_set_of_boxes_is_one_question_with_options():
    heard, ack = greenhouse.extract_fields(BOX_GROUPS)
    assert (heard.dom_id, heard.field.label, heard.field.kind) == (
        HEARD,
        "How did you hear about Twilio?",
        "checkboxes",
    )
    assert heard.field.required
    assert heard.field.options == ["Careers Website", "LinkedIn", "Other"]
    assert ack.field.options == ["Acknowledge"]


def test_boxes_are_ticked_one_at_a_time_in_the_browser():
    sync_api = pytest.importorskip("playwright.sync_api")
    try:
        with sync_api.sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page()
            page.set_content(BOX_GROUPS)
            session = greenhouse.GreenhouseSession(page, None)
            before = [m[0] for m in session.missing_required()]
            plan = [Action("tick_option", HEARD, "Other"), Action("tick_option", ACK, "Lie")]
            rejected = session.fill(plan)
            checked = page.eval_on_selector_all("input:checked", "els => els.map(e => e.id)")
            browser.close()
    except sync_api.Error as e:  # no browser installed here
        pytest.skip(str(e))
    assert before == [HEARD, ACK]
    assert checked == [f"{HEARD}_2"]
    assert rejected == [ACK]
