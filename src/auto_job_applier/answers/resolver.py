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
from .bank import AnswerBank, normalize, option_key
from .facts import candidate_facts

Writer = Callable[[FormField, Job, str], ResolvedAnswer]
Value = str | bool | list[str] | None  # a list: choices in the order to try them
_BLANK = object()  # a source deliberately answers "leave this empty"

_WORK_AUTH = re.compile(
    r"sponsor|visa|authori[sz]ed to work|work authori[sz]ation|authori[sz]ation to work"
    r"|eligib\w* (to|for) (work|employment)|legally (permitted|allowed|eligible|able)"
    r"|citizen|immigration|right to work|\bu ?s person\b|\bitar\b|export control|h ?1 ?b"
    r"|\bopt\b(?! (in|out))|\bcpt\b|green card|permanent resid"
)
# Questions about a specific status. A generic "yes, I'm authorized" answer must never reach them.
_STATUS_SPECIFIC = re.compile(r"citizen|u ?s person|itar|export|h ?1 ?b|green card|permanent")
_OPEN_ENDED = re.compile(
    r"\b(what|which|describe|explain|elaborate|details?|provide|tell us)\b|^if\b"
)
_LONG_FORM = re.compile(
    r"cover letter|\bwhy\b|what interests you|interested in (this|our|working)|motivat"
)
_WORKED_HERE = re.compile(
    r"(previously|ever|formerly|before|in the past)\b.*\b(employed|worked|work)\b"
    r"|\b(employed|worked)\b.*\b(before|previously|in the past)\b|former employee"
)
_FAMILY_THERE = re.compile(
    r"\b(family|relatives?|related to|spouse|household)\b.*\b(employ\w*|work\w*|director\w*)\b"
    r"|\b(employ\w*|work\w*)\b.*\b(family|relatives?|related to)\b"
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
        (
            r"\b(located|locat\w+|reside|residing|live|living|based) (currently )?in the "
            r"(us|u s|usa|united states)\b",
            lambda: (
                normalize(a.country) in ("united states", "us", "usa", "united states of america")
            ),
        ),
        (
            r"current location|where are you (currently )?located|^location( city)?$",
            lambda: f"{a.city}, {a.state}",
        ),
        (r"\bcity\b", lambda: a.city),
        (r"\bstate\b|province", lambda: a.state),
        (r"zip|postal", lambda: a.postal_code),
        (r"\bcountry\b", lambda: a.country),
        (r"linkedin", lambda: p.links.linkedin),
        (r"github", lambda: p.links.github),
        (r"website|portfolio|personal site", lambda: p.links.portfolio),
        # Greenhouse: "Education: Start date month", "Education: End date year".
        (r"^education start date month", lambda: edu and _month(edu.start)),
        (r"^education start date year", lambda: edu and edu.start and edu.start[:4]),
        (r"^education end date month", lambda: edu and _month(edu.graduation)),
        (r"^education end date year", lambda: edu and edu.graduation[:4]),
        (r"school|university|college|institution", lambda: edu and [edu.school, *edu.school_also]),
        (r"\bdegree\b", lambda: edu and [edu.degree, *edu.degree_also]),
        (
            r"major|field of study|discipline",
            lambda: edu and [edu.major, *edu.major_also],
        ),
        (r"graduation year|year of graduation", lambda: edu and edu.graduation[:4]),
        (r"graduat", lambda: edu and edu.graduation),
        (r"\bgpa\b", lambda: edu and edu.gpa),
        # Greenhouse labels its employment section "Employment: Company name" (T7).
        (
            r"(current|most recent) (company|employer)|^employment company",
            lambda: e.current_company,
        ),
        (r"(current|most recent) (job )?title|^employment (job )?title", lambda: e.current_title),
        # The employment section holds only the current job, so its "Current role" box is ticked.
        (r"^employment current role", lambda: True if e.current_company else None),
        (
            r"^(how many )?(total )?years of (professional |work )?experience( do you have)?$",
            lambda: f"{e.years_experience:g}",
        ),
        (r"pronoun", lambda: d.pronouns),  # before "gender": "your gender pronouns"
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


def _names_employer(label: str, job: Job) -> bool:
    """The question is about this company: it names it, or says "us"."""
    company = normalize(job.company)
    return bool(company and company in label) or bool(
        re.search(r"\b(us|our (company|organization|team)|this company)\b", label)
    )


def _by_polarity(options: list[str], yes: bool) -> str | None:
    """For sentence options ("I have not previously been employed at Affirm"): "no" is the
    one negated option. "Yes" often has several ("as a full-time employee", "as an intern"),
    so it is picked only when there is exactly one."""
    hits = [o for o in options if normalize(o) and _negated(o) != yes]
    return hits[0] if len(hits) == 1 else None


def _tie_to_company(label: str, job: Job, p: Profile) -> bool | None:
    """Questions about the owner's own ties to this company, answered from the profile:
    relatives working there, or having worked there before. None = not such a question."""
    if not _names_employer(label, job):
        return None
    e, here = p.experience, normalize(job.company)
    if _FAMILY_THERE.search(label):
        return here in [normalize(c) for c in e.family_employed_at]
    if _WORKED_HERE.search(label):
        return here in [normalize(c) for c in [*e.past_employers, e.current_company or ""] if c]
    return None


def _month(yyyy_mm: str | None) -> list[str] | None:
    """A month as forms spell it, in the order to try: "August", "Aug", "08", "8"."""
    if not yyyy_mm or len(yyyy_mm) < 7:
        return None
    m = int(yyyy_mm[5:7])
    name = date(2000, m, 1).strftime("%B")
    return list(dict.fromkeys([name, name[:3], f"{m:02d}", str(m)]))


def _preference_value(label: str, field: FormField, p: Profile) -> Value | object:
    """Preset answers; None = not a preference. Optional salary is deliberately `_BLANK`."""
    pref = p.preferences
    if label.startswith(("employment ", "education ")):
        return None  # "Employment: Start date" is a past job's date, not the preferred start
    if re.search(r"start date|when can you start|available to start|earliest start", label):
        return pref.start_date
    if re.search(
        r"(how|where) did you (first )?(hear|find|learn|come across)|how you (heard|found|learned)",
        label,
    ):
        return pref.heard_about_us
    if re.search(r"relocat", label):
        return pref.willing_to_relocate
    if re.search(r"salary|compensation|pay expectation", label):
        # R12/D6: blank unless required; market lookup arrives in T13, the floor is the fallback.
        return str(pref.salary_floor) if field.required else _BLANK
    return None


def is_work_auth(question: str) -> bool:
    """Work authorization and sponsorship: answered from the profile only (R11)."""
    return bool(_WORK_AUTH.search(normalize(question)))


def _explains(f: FormField, label: str, fact: Value, p: Profile) -> bool:
    """Use the owner's explanation: open-ended free text that a yes/no fact answers poorly."""
    return bool(
        p.work_authorization.explanation
        and not f.options
        and not isinstance(fact, str)  # "What is your visa status?" keeps the visa status
        and _OPEN_ENDED.search(label)
        and not _STATUS_SPECIFIC.search(label)
    )


def _approved_work_auth(f: FormField, p: Profile) -> ResolvedAnswer | None:
    """The one option the owner approved word for word in the profile; None if none is offered."""
    approved = {normalize(a) for a in p.work_authorization.approved_answers}
    hits = [o for o in f.options if normalize(o) in approved]
    if not hits:
        return None
    if len(hits) > 1:
        return _unresolved(f, "several approved work authorization answers fit: answer at review")
    return _answer(f, hits[0], "profile", "approved in the profile")


def _work_auth_value(label: str, p: Profile) -> Value:
    wa = p.work_authorization
    needs = wa.requires_sponsorship_now or wa.requires_sponsorship_future
    if re.search(r"without (visa |any |the need for )?sponsor", label):
        return wa.authorized_to_work_us and not needs
    if "sponsor" in label:
        return needs
    if re.search(r"authori[sz]|eligib|legally", label) and not _STATUS_SPECIFIC.search(label):
        return wa.authorized_to_work_us
    if re.search(r"visa status|immigration status|current visa", label):
        return wa.visa_status
    return None


def _negated(text: str) -> bool:
    # "Non-binary", "non-immigrant visa": a prefix, not a "no"
    return bool(set(normalize(re.sub(r"\bnon-", "non", text, flags=re.I)).split()) & _NEGATIONS)


def match_option(value: Value, field: FormField) -> str | None:
    """The form's option for `value`, or the text itself for free-text fields."""
    if value is None:
        return None
    if isinstance(value, list):  # the first choice the form offers
        return next((m for v in value if (m := match_option(v, field)) is not None), None)
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
    spelled = [o for o in field.options if option_key(o) == option_key(value)]
    if len(spelled) == 1:  # "Bachelor's" for "Bachelor's Degree"
        return spelled[0]
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
            same_polarity = _negated(o) == _negated(value)
            if ow and same_polarity and (words <= ow or ow <= words):
                hits.append(o)
    return hits[0] if len(hits) == 1 else None


def _answer(field: FormField, value: Value, source: str, reason: str = "") -> ResolvedAnswer:
    if isinstance(value, list) and field.kind == "select" and not field.options:
        # Options load as you type: the engine tries each choice on the page, in order.
        first, *rest = value
        return ResolvedAnswer(field, first, source, True, reason, rest)
    matched = match_option(value, field)
    if value is not None and matched is None:
        return ResolvedAnswer(field, None, source, False, f"{value!r} matches no option")
    return ResolvedAnswer(field, matched, source, True, reason)


def _unresolved(field: FormField, reason: str, source: str = "none") -> ResolvedAnswer:
    return ResolvedAnswer(field, None, source, False, reason)


def _from_known_sources(
    f: FormField, label: str, profile: Profile, bank: AnswerBank
) -> ResolvedAnswer | None:
    """profile → preferences → bank (R10): the first answer that fits the field wins.

    A dropdown whose options load only as you type (school, degree) can't be checked against
    the profile here, so an option the owner picked at review comes first and the profile's
    choices follow as alternatives for the engine to try.
    """
    from_profile = ("profile", lambda: _profile_value(label, profile))
    from_preferences = ("preferences", lambda: _preference_value(label, f, profile))
    from_bank = ("bank", lambda: bank.lookup(f.label))
    sources: list[tuple[str, Callable[[], Value | object]]] = (
        [from_bank, from_profile, from_preferences]
        if f.kind == "select" and not f.options
        else [from_profile, from_preferences, from_bank]
    )
    for source, get in sources:
        v = get()
        if v is None:
            continue
        ans = _answer(f, None if v is _BLANK else v, source)
        if ans.confident:
            if source == "bank" and f.kind == "select" and not f.options:
                more = _profile_value(label, profile)
                ans.alternatives = (
                    [c for c in more if c != ans.value] if isinstance(more, list) else []
                )
            return ans
    return None


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
        return {i: _unresolved(f, f"no LLM answer: {e}", "llm") for i, f in pending.items()}
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
            if (ans := _approved_work_auth(f, profile)) is not None:
                results[i] = ans
                continue
            value = _work_auth_value(label, profile)
            if _explains(f, label, value, profile):
                value = profile.work_authorization.explanation
            ans = _answer(f, value, "profile")
            if ans.value is None:
                ans = _unresolved(
                    f, "work authorization question not covered by the profile: answer at review"
                )
            results[i] = ans
            continue
        if (tie := _tie_to_company(label, job, profile)) is not None:
            ans = _answer(f, tie, "profile")
            if ans.value is None and (option := _by_polarity(f.options, tie)):
                ans = _answer(f, option, "profile", "the option that says so")
            if ans.confident and ans.value is not None:
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
        if (ans := _from_known_sources(f, label, profile, bank)) is not None:
            results[i] = ans
        elif f.required:
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
