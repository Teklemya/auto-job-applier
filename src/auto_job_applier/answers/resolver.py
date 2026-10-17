"""Answer every form field (R10) and decide whether the packet may be submitted (D1).

Order: profile → preferences → answer bank → LLM, and every answer records its source.
Work-authorization and sponsorship questions are answered from the profile or not at all:
they never reach the bank or the LLM (R11). Optional fields the first three sources can't
answer are left blank, which keeps LLM calls down (D7). Cover letters and "why us" answers go
to the writer (T6), and only when required.
"""

import re
from collections.abc import Callable
from datetime import date

from pydantic import BaseModel

from ..llm import LLM, LLMError, QuotaExhausted
from ..models import FormField, Job, ResolvedAnswer
from ..profile import DECLINE, Profile
from .bank import AnswerBank, normalize
from .facts import candidate_facts

Writer = Callable[[FormField, Job, str], ResolvedAnswer]
Value = str | bool | None

_WORK_AUTH = re.compile(
    r"sponsor|visa|authori[sz]ed to work|work authori[sz]ation|authori[sz]ation to work"
    r"|eligib\w* (to|for) (work|employment)|legally (permitted|allowed|eligible|able)"
    r"|citizen|immigration|right to work|\bu ?s person\b|itar|export control|h ?1 ?b"
    r"|\bopt\b(?! (in|out))|\bcpt\b|green card|permanent resid"
)
_LONG_FORM = re.compile(
    r"cover letter|\bwhy\b|what interests you|interested in (this|our|working)|motivat"
)
_NEGATIONS = {"no", "not", "non", "never", "don", "dont", "doesn", "isn", "aren", "won"}
_DECLINE_OPTION = re.compile(
    r"decline|not wish|don ?t wish|prefer not|choose not|not to (say|answer)"
)

SYSTEM = """\
You fill in job application questions for one candidate, whose resume is attached.
Answer only from the resume, the candidate facts and the job description given.
If the answer depends on a fact that is not stated there (age, criminal history, relatives at
the company, a personal preference, a certification, anything about the candidate's life),
set confident to false. Never invent or infer such facts. For a question with options, value
must be exactly one of the options. Keep text answers short and factual. Return one answer per
question id."""


class FieldAnswer(BaseModel):
    id: int
    value: str
    confident: bool
    reason: str


class FieldAnswers(BaseModel):
    answers: list[FieldAnswer]


def _profile_value(label: str, p: Profile) -> Value:
    """Facts from the profile, by question label (already normalized). None = not a fact."""
    c, a, e, d = p.contact, p.address, p.experience, p.demographics
    edu = p.education[0] if p.education else None
    rules: list[tuple[str, Callable[[], Value]]] = [
        (r"preferred (first )?name", lambda: c.preferred_name or c.first_name),
        (r"first name|given name", lambda: c.first_name),
        (r"last name|family name|surname", lambda: c.last_name),
        (r"full name|legal name|^name$", lambda: f"{c.first_name} {c.last_name}"),
        (r"e ?mail", lambda: c.email),
        (r"phone (device )?type|device type", lambda: c.phone_device_type),
        (r"phone|mobile", lambda: c.phone),
        (r"address line 2|apartment|suite", lambda: a.line2 or None),
        (r"address", lambda: a.line1),
        (r"current location|where are you (currently )?located", lambda: f"{a.city}, {a.state}"),
        (r"\bcity\b", lambda: a.city),
        (r"\bstate\b|province", lambda: a.state),
        (r"zip|postal", lambda: a.postal_code),
        (r"\bcountry\b", lambda: a.country),
        (r"linkedin", lambda: p.links.linkedin),
        (r"github", lambda: p.links.github),
        (r"website|portfolio|personal site", lambda: p.links.portfolio),
        (r"school|university|college|institution", lambda: edu and edu.school),
        (r"\bdegree\b", lambda: edu and edu.degree),
        (r"major|field of study|discipline", lambda: edu and edu.major),
        (r"graduation year|year of graduation", lambda: edu and edu.graduation[:4]),
        (r"graduat", lambda: edu and edu.graduation),
        (r"\bgpa\b", lambda: edu and edu.gpa),
        (r"(current|most recent) (company|employer)", lambda: e.current_company),
        (r"(current|most recent) (job )?title", lambda: e.current_title),
        (
            r"^(how many )?(total )?years of (professional |work )?experience( do you have)?$",
            lambda: f"{e.years_experience:g}",
        ),
        (r"hispanic|latin", lambda: d.hispanic_latino),
        (r"\brace\b|ethnicity", lambda: d.race_ethnicity),
        (r"gender", lambda: d.gender),
        (r"veteran", lambda: d.veteran_status),
        (r"disabilit", lambda: d.disability_status),
    ]
    for pattern, get in rules:
        if re.search(pattern, label):
            return get()
    return None


def _preference_value(label: str, field: FormField, p: Profile) -> tuple[bool, Value]:
    """(matched, value). A matched optional salary is deliberately blank."""
    pref = p.preferences
    if re.search(r"start date|when can you start|available to start|earliest start", label):
        return True, pref.start_date
    if re.search(r"how did you (hear|find|learn)|where did you (hear|find|learn)", label):
        return True, pref.heard_about_us
    if re.search(r"relocat", label):
        return True, pref.willing_to_relocate
    if re.search(r"salary|compensation|pay expectation", label):
        # D6: market lookup arrives in T13; the owner's floor is the fallback.
        return True, str(pref.salary_floor) if field.required else None
    return False, None


def _work_auth_value(label: str, p: Profile) -> Value:
    wa = p.work_authorization
    needs = wa.requires_sponsorship_now or wa.requires_sponsorship_future
    if re.search(r"without (visa |any |the need for )?sponsor", label):
        return wa.authorized_to_work_us and not needs
    if "sponsor" in label:
        return needs
    if re.search(r"authori[sz]|eligib|legally", label) and not re.search(
        r"citizen|u ?s person|itar|export|h ?1 ?b|green card|permanent", label
    ):
        return wa.authorized_to_work_us
    if re.search(r"visa status|immigration status|current visa", label):
        return wa.visa_status
    return None


def match_option(value: Value, field: FormField) -> str | None:
    """The form's option for `value`, or the text itself for free-text fields."""
    if value is None:
        return None
    if isinstance(value, bool):
        word = "yes" if value else "no"
        if not field.options:
            return word.capitalize()
        hits = [o for o in field.options if normalize(o).split()[:1] == [word]]
        return hits[0] if len(hits) == 1 else None
    if not field.options:
        return value
    v = normalize(value)
    for o in field.options:
        if normalize(o) == v:
            return o
    if value == DECLINE:
        hits = [o for o in field.options if _DECLINE_OPTION.search(normalize(o))]
    else:
        # Whole words, either way round ("No" must not match "Latino"), with the same polarity
        # ("I am not a veteran" must not match "I am a veteran"). Placeholders like "--" have
        # no words and match nothing.
        words = set(v.split())
        hits = []
        for o in field.options:
            ow = set(normalize(o).split())
            same_polarity = bool(ow & _NEGATIONS) == bool(words & _NEGATIONS)
            if ow and same_polarity and (words <= ow or ow <= words):
                hits.append(o)
    return hits[0] if len(hits) == 1 else None


def _answer(field: FormField, value: Value, source: str, reason: str = "") -> ResolvedAnswer:
    matched = match_option(value, field)
    if value is not None and matched is None:
        return ResolvedAnswer(field, None, source, False, f"{value!r} matches no option")
    return ResolvedAnswer(field, matched, source, True, reason)


def _unresolved(field: FormField, reason: str, source: str = "none") -> ResolvedAnswer:
    return ResolvedAnswer(field, None, source, False, reason)


def _ask_llm(
    pending: dict[int, FormField], profile: Profile, model: LLM, job: Job, jd: str, today: str
) -> dict[int, ResolvedAnswer]:
    questions = "\n".join(
        f"[{i}] {f.label} ({f.kind}{'; options: ' + ' | '.join(f.options) if f.options else ''})"
        for i, f in pending.items()
    )
    prompt = (
        f"{candidate_facts(profile, today)}\n\n"
        f"Job: {job.role} at {job.company}\nJob description:\n{jd[:10_000]}\n\n"
        f"Questions:\n{questions}"
    )
    try:
        out = model.generate(
            system=SYSTEM,
            prompt=prompt,
            schema=FieldAnswers,
            attachments=[(profile.resume_path.read_bytes(), "application/pdf")],
        )
    except (QuotaExhausted, LLMError) as e:
        return {i: _unresolved(f, f"LLM unavailable: {e}", "llm") for i, f in pending.items()}
    by_id = {a.id: a for a in out.answers}
    results = {}
    for i, f in pending.items():
        a = by_id.get(i)
        if a is None or not a.value.strip():
            results[i] = _unresolved(f, "LLM gave no answer", "llm")
        elif not a.confident:
            results[i] = _unresolved(f, f"LLM unsure: suggested {a.value!r} ({a.reason})", "llm")
        else:
            results[i] = _answer(f, a.value.strip(), "llm", a.reason)
    return results


def resolve(
    fields: list[FormField],
    profile: Profile,
    *,
    bank: AnswerBank,
    llm: LLM,
    job: Job,
    jd: str,
    writer: Writer | None = None,
    today: Callable[[], str] = lambda: date.today().isoformat(),
) -> list[ResolvedAnswer]:
    results: dict[int, ResolvedAnswer] = {}
    for_llm: dict[int, FormField] = {}
    for i, f in enumerate(fields):
        label = normalize(f.label)
        if _WORK_AUTH.search(label):
            ans = _answer(f, _work_auth_value(label, profile), "profile")
            if ans.value is None:
                ans = _unresolved(
                    f, "work authorization question not covered by the profile: answer at review"
                )
            results[i] = ans
            continue
        if f.kind == "file":
            if re.search(r"resume|\bcv\b", label):
                results[i] = _answer(f, str(profile.resume_path), "profile")
                continue
            if not _LONG_FORM.search(label):
                results[i] = (
                    _unresolved(f, "file upload not in the profile")
                    if f.required
                    else _answer(f, None, "none")
                )
                continue
        if _LONG_FORM.search(label):
            if not f.required:
                results[i] = _answer(f, None, "none", "optional long-form answer left blank")
            elif writer is None:
                results[i] = _unresolved(f, "needs a written answer")
            else:
                results[i] = writer(f, job, jd)
            continue
        if (v := _profile_value(label, profile)) is not None:
            ans = _answer(f, v, "profile")
            if ans.confident:
                results[i] = ans
                continue
        matched, v = _preference_value(label, f, profile)
        if matched:
            ans = _answer(f, v, "preferences")
            if ans.confident:
                results[i] = ans
                continue
        if (v := bank.lookup(f.label)) is not None:
            ans = _answer(f, v, "bank")
            if ans.confident:
                results[i] = ans
                continue
        if f.required:
            for_llm[i] = f
        else:
            results[i] = _answer(f, None, "none")
    if for_llm:
        results.update(_ask_llm(for_llm, profile, llm, job, jd, today()))
    return [results[i] for i in range(len(fields))]


def gate(answers: list[ResolvedAnswer]) -> list[str]:
    """D1: labels of required fields without a confident answer. Empty = may submit."""
    return [
        a.field.label for a in answers if a.field.required and (not a.confident or a.value is None)
    ]
