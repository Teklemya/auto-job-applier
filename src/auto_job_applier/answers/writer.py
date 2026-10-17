"""Cover letters and "why us" answers (R13), written from the resume and the job description.

Only called for required fields (D7). The model reports facts it would need but doesn't have;
any such gap, or an overlong answer, leaves the field unanswered so the job becomes a draft.
"""

from pydantic import BaseModel

from ..llm import LLM, LLMError, QuotaExhausted
from ..models import FormField, Job, ResolvedAnswer
from ..profile import Profile
from .resolver import Writer

LETTER_WORDS = 250
SHORT_WORDS = 120
SLACK = 1.2  # tolerate a little overshoot before rejecting

SYSTEM = """\
You write job application text for one early-career software engineer, whose resume is
attached. Write like a real person: plain, specific and warm. No cliches ("I am thrilled",
"passionate", "fast-paced", "leverage"), no em dashes, no bullet points, no placeholders.

Ground every claim in the resume. Connect one or two concrete things from the resume to what
the job description says the team does. Do not invent projects, numbers, employers, dates or
skills. If a good answer would need a fact that the resume does not give (for example years
with a specific tool, or a personal connection to the company), set confident to false and
list those facts in missing_facts."""


class Draft(BaseModel):
    text: str
    confident: bool
    missing_facts: list[str]


def _instructions(field: FormField, job: Job, name: str) -> tuple[str, int]:
    if "cover letter" in field.label.lower():
        return (
            f"Write a cover letter for the {job.role} role at {job.company}, at most "
            f"{LETTER_WORDS} words in three short paragraphs. Start with 'Dear {job.company} "
            f"hiring team,' and sign off with '{name}'.",
            LETTER_WORDS,
        )
    return (
        f'Answer the application question "{field.label}" for the {job.role} role at '
        f"{job.company} in at most {SHORT_WORDS} words, in the first person.",
        SHORT_WORDS,
    )


def make_writer(profile: Profile, model: LLM) -> Writer:
    name = f"{profile.contact.first_name} {profile.contact.last_name}"

    def write(field: FormField, job: Job, jd: str) -> ResolvedAnswer:
        task, limit = _instructions(field, job, name)
        prompt = f"{task}\n\nJob description:\n{jd[:10_000]}"
        try:
            d = model.generate(
                system=SYSTEM,
                prompt=prompt,
                schema=Draft,
                attachments=[(profile.resume_path.read_bytes(), "application/pdf")],
            )
        except (QuotaExhausted, LLMError) as e:
            return ResolvedAnswer(field, None, "llm", False, f"LLM unavailable: {e}")
        text = d.text.strip()
        if not d.confident or d.missing_facts:
            missing = ", ".join(d.missing_facts) or "unspecified facts"
            return ResolvedAnswer(field, None, "llm", False, f"writer needs: {missing}")
        if not text:
            return ResolvedAnswer(field, None, "llm", False, "writer returned nothing")
        if len(text.split()) > limit * SLACK:
            return ResolvedAnswer(
                field, None, "llm", False, f"too long ({len(text.split())} > {limit} words)"
            )
        return ResolvedAnswer(field, text, "llm", True, "written from resume + job description")

    return write
