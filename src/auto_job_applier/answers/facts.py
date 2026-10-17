"""Candidate facts given to every LLM prompt, so answers use the right name and tense."""

from ..profile import Profile


def candidate_facts(p: Profile, today: str) -> str:
    c, a, x = p.contact, p.address, p.experience
    lines = [
        f"Today is {today}.",
        f"Name: {c.preferred_name or c.first_name} {c.last_name}.",
        f"Location: {a.city}, {a.state}.",
    ]
    for e in p.education:
        when = (
            f"graduated {e.graduation}"
            if e.graduation <= today[:7]
            else f"graduates {e.graduation} (not yet graduated)"
        )
        lines.append(f"Education: {e.degree} in {e.major}, {e.school}, {when}.")
    lines.append(f"Years of experience: {x.years_experience:g}.")
    if x.current_title and x.current_company:
        lines.append(f"Current job: {x.current_title} at {x.current_company}.")
    return "Candidate facts:\n" + "\n".join(lines)
