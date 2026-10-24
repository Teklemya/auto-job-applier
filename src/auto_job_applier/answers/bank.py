"""Answer bank: questions the owner answered before, reused when they come up again (R10)."""

import re
from difflib import SequenceMatcher

from ..store import JobStore

MIN_SIMILARITY = 0.9
_NOISE = re.compile(r"\b(required|optional)\b")


def normalize(question: str) -> str:
    text = re.sub(r"[^a-z0-9]+", " ", question.lower())
    return " ".join(_NOISE.sub(" ", text).split())


_OPTION_SYNONYMS = [
    (re.compile(r"\b(b s|bs|bsc|b sc)\b"), "bachelor of science"),
    (re.compile(r"\b(b a|ba)\b"), "bachelor of arts"),
    (re.compile(r"\b(m s|ms|msc|m sc)\b"), "master of science"),
    (re.compile(r"\bbachelor s\b|\bbachelors\b"), "bachelor"),
    (re.compile(r"\bmaster s\b|\bmasters\b"), "master"),
    (re.compile(r"\bdegree\b"), ""),
]


def option_key(text: str) -> str:
    """Dropdown text with spelling differences folded: "Bachelor's Degree", "Bachelors" and
    "Bachelor" are one key, as are "BS" and "Bachelor of Science"."""
    key = normalize(text)
    for pattern, repl in _OPTION_SYNONYMS:
        key = pattern.sub(repl, key)
    return " ".join(key.split())


def _numbers(text: str) -> list[str]:
    return re.findall(r"\d+", text)


class AnswerBank:
    def __init__(self, store: JobStore):
        self.store = store

    def save(self, question: str, value: str) -> None:
        self.store.save_bank_entry(normalize(question), question, value)

    def lookup(self, question: str) -> str | None:
        """Best stored answer for a near-identical question. Numbers must match exactly, so
        "at least 18" never answers "at least 21"."""
        key = normalize(question)
        best, best_score = None, MIN_SIMILARITY
        for stored, value in self.store.bank_entries():
            if stored == key:
                return value
            if _numbers(stored) != _numbers(key):
                continue
            score = SequenceMatcher(None, stored, key).ratio()
            if score >= best_score:
                best, best_score = value, score
        return best
