import pytest

from auto_job_applier.engines.base import Action, PageField, fill_plan
from auto_job_applier.models import FormField, ResolvedAnswer


def page_field(dom_id, kind="text", label="Q", required=True):
    return PageField(dom_id, FormField(label=label, kind=kind, required=required))


def answer(pf, value, confident=True):
    return ResolvedAnswer(field=pf.field, value=value, source="profile", confident=confident)


def test_text_answers_are_typed():
    pf = page_field("first_name")
    assert fill_plan([pf], [answer(pf, "Jane")]) == [Action("type", "first_name", "Jane")]


def test_dropdown_answers_are_chosen():
    pf = page_field("gender", kind="select")
    assert fill_plan([pf], [answer(pf, "Female")]) == [Action("choose", "gender", "Female")]


def test_long_text_is_typed_too():
    pf = page_field("question_1", kind="textarea")
    assert fill_plan([pf], [answer(pf, "Hello")]) == [Action("type", "question_1", "Hello")]


def test_files_on_disk_are_uploaded(tmp_path):
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF")
    pf = page_field("resume", kind="file")
    assert fill_plan([pf], [answer(pf, str(resume))]) == [Action("upload", "resume", str(resume))]


def test_written_text_for_a_file_field_is_pasted():
    pf = page_field("cover_letter", kind="file")
    plan = fill_plan([pf], [answer(pf, "Dear team, ...")])
    assert plan == [Action("paste", "cover_letter", "Dear team, ...")]


@pytest.mark.parametrize("value", ["Yes", "true"])
def test_checkboxes_are_ticked_for_yes(value):
    pf = page_field("current-role-0_1", kind="checkbox")
    assert fill_plan([pf], [answer(pf, value)]) == [Action("check", "current-role-0_1", "")]


def test_checkboxes_answered_no_are_left_alone():
    pf = page_field("current-role-0_1", kind="checkbox")
    assert fill_plan([pf], [answer(pf, "No")]) == []


def test_blank_answers_leave_the_field_alone():
    pf = page_field("question_2")
    assert fill_plan([pf], [answer(pf, None, confident=False)]) == []


def test_unconfident_answers_are_left_blank_for_the_owner():
    # Never guess (D1): a blank, highlighted field at review beats a plausible wrong value.
    pf = page_field("question_3")
    assert fill_plan([pf], [answer(pf, "maybe", confident=False)]) == []


def test_fields_and_answers_must_line_up():
    with pytest.raises(ValueError):
        fill_plan([page_field("a"), page_field("b")], [answer(page_field("a"), "x")])
