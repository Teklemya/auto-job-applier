"""Greenhouse job boards (job-boards.greenhouse.io): read the form, fill it, stage a draft.

The form is built in the browser, so fields are read from the rendered page: every input with
an id, its `<label for>`, and `aria-required`. Dropdowns are React comboboxes
(`role="combobox"`). The public questions API (boards-api `?questions=true`) uses the same names
as the page ids and supplies the dropdown options. Employment and education fields get their
section in the label ("Employment: Title") so the resolver can tell them apart.
"""

import contextlib
import html as html_lib
import random
import re
from collections.abc import Callable
from html.parser import HTMLParser
from pathlib import Path

from ..geo import US_STATES
from ..jd_fetch import _get_json, api_url
from ..models import FormField, Job
from .base import Action, PageField

_SECTIONS = {"employment--container": "Employment", "education--container": "Education"}
# Not questions: the paste-instead-of-upload boxes, the captcha token, the phone country search.
_SKIP_IDS = {"resume_text", "cover_letter_text"}
_SKIP_PREFIXES = ("g-recaptcha-response", "iti-")


def _api_options(api: dict | None) -> dict[str, list[str]]:
    """Field name → option labels, from questions, EEO compliance and demographic sections."""
    if not api:
        return {}
    options: dict[str, list[str]] = {}
    questions = list(api.get("questions") or []) + list(api.get("location_questions") or [])
    for section in api.get("compliance") or []:
        questions += section.get("questions") or []
    for q in questions:
        for f in q.get("fields") or []:
            if f.get("values"):
                options[f["name"]] = [v["label"] for v in f["values"]]
    demographic = api.get("demographic_questions") or {}
    for q in demographic.get("questions") or []:
        options[str(q["id"])] = [o["label"] for o in q.get("answer_options") or []]
    return options


class _FormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.inputs: list[dict] = []  # in page order: id, kind, required, section
        self.labels: dict[str, str] = {}  # dom id → label text; the first label wins
        self.file_required: dict[str, bool] = {}
        self._label_for: str | None = None
        self._label_tag = ""  # "label", or "div" for an upload group's label
        self._label_text: list[str] = []
        self._div_depth = 0
        self._section: tuple[str, int] | None = None  # (name, div depth where it opened)
        self._disabled_depth: int | None = None  # inside a greyed-out dropdown

    def handle_starttag(self, tag, attrs):
        a = {k: v or "" for k, v in attrs}
        if tag == "div":
            self._div_depth += 1
            classes = a.get("class", "").split()
            for cls in classes:
                if cls in _SECTIONS and self._section is None:
                    self._section = (_SECTIONS[cls], self._div_depth)
            if "select__control--is-disabled" in classes and self._disabled_depth is None:
                self._disabled_depth = self._div_depth
            labelled_by = a.get("aria-labelledby", "")
            if a.get("role") == "group" and labelled_by.startswith("upload-label-"):
                dom_id = labelled_by.removeprefix("upload-label-")
                self.file_required[dom_id] = a.get("aria-required") == "true"
            # The group label ("Resume/CV") comes before the button's own "Attach" label.
            if a.get("id", "").startswith("upload-label-"):
                self._start_label(a["id"].removeprefix("upload-label-"), "div")
        elif tag == "label" and a.get("for"):
            self._start_label(a["for"], "label")
        elif tag in ("input", "textarea") and a.get("id"):
            self._add_input(tag, a)

    def handle_endtag(self, tag):
        if self._label_for is not None and tag == self._label_tag:
            self._end_label()
        if tag == "div":
            if self._section and self._section[1] == self._div_depth:
                self._section = None
            if self._disabled_depth == self._div_depth:
                self._disabled_depth = None
            self._div_depth -= 1

    def handle_data(self, data):
        if self._label_for is not None:
            self._label_text.append(data)

    def _start_label(self, dom_id: str, tag: str) -> None:
        self._label_for, self._label_tag, self._label_text = dom_id, tag, []

    def _end_label(self) -> None:
        text = " ".join("".join(self._label_text).split()).rstrip("*").strip()
        if text:
            self.labels.setdefault(self._label_for, text)
        self._label_for = None

    def _add_input(self, tag: str, a: dict) -> None:
        dom_id, kind = a["id"], a.get("type", "text")
        if kind in ("hidden", "search") or dom_id in _SKIP_IDS:
            return
        # Greyed out by another answer ("Current role" disables the end date): not asked.
        greyed = "input--disabled" in a.get("class", "").split() or "disabled" in a
        if greyed or self._disabled_depth is not None:
            return
        if dom_id.startswith(_SKIP_PREFIXES):
            return
        if a.get("role") == "combobox":
            kind = "select"
        elif tag == "textarea":
            kind = "textarea"
        elif kind not in ("file", "checkbox"):
            kind = "text"
        self.inputs.append(
            {
                "id": dom_id,
                "kind": kind,
                "required": a.get("aria-required") == "true",
                "section": self._section[0] if self._section else None,
            }
        )


def extract_fields(page_html: str, api: dict | None = None) -> list[PageField]:
    parser = _FormParser()
    parser.feed(page_html)
    options = _api_options(api)
    fields, seen = [], set()
    for i in parser.inputs:
        dom_id = i["id"]
        label = parser.labels.get(dom_id)
        if not label or dom_id in seen:
            continue
        seen.add(dom_id)
        required = i["required"]
        if i["kind"] == "file":
            required = parser.file_required.get(dom_id, False)
        if i["section"]:
            label = f"{i['section']}: {label}"
        field = FormField(
            label=html_lib.unescape(label),
            kind=i["kind"],
            required=required,
            options=options.get(dom_id, []),
        )
        fields.append(PageField(dom_id, field))
    return fields


def _words(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())


def pick_option(value: str, options: list[str]) -> str | None:
    """The dropdown option meaning `value`, or None when no single option clearly fits.

    Places come back spelled out ("Kansas City, Missouri, United States"), so a value like
    "Kansas City, MO" matches on the city and then on the state, by name or abbreviation.
    """
    wanted = _words(value)
    exact = [o for o in options if _words(o) == wanted]
    if exact:
        return exact[0]
    head, *rest = [_words(part) for part in value.split(",")]
    hits = [o for o in options if _words(o).startswith(head)]
    for part in rest:
        names = {part, _words(US_STATES.get(part.upper(), part))}
        narrowed = [o for o in hits if names & set(_words(p) for p in o.split(","))]
        hits = narrowed or hits
    return hits[0] if len(hits) == 1 else None


def fetch_questions(job: Job, get: Callable[[str], dict] = _get_json) -> dict:
    return get(api_url(job) + "?questions=true")


# --- the live page ----------------------------------------------------------------------------

FORM_READY = "#first_name"
_CHALLENGES = "iframe[src*='recaptcha'][src*='bframe'], iframe[src*='hcaptcha'][src*='challenge']"


class GreenhouseSession:
    """The application form open in the tool's browser (see apply.FormSession)."""

    def __init__(self, page, api: dict | None):
        self.page, self.api = page, api

    @classmethod
    def open(cls, page, job: Job, api: dict | None) -> "GreenhouseSession":
        page.goto(job.url, wait_until="domcontentloaded")
        # No form (a closed posting or a changed page) leaves fields() empty → NO_FORM.
        with contextlib.suppress(Exception):
            page.wait_for_selector(FORM_READY, timeout=30_000)
        return cls(page, api)

    def _box(self, dom_id: str):
        return self.page.locator(f"[id='{dom_id}']")

    def _pause(self) -> None:
        self.page.wait_for_timeout(random.randint(120, 400))

    def fields(self) -> list[PageField]:
        return extract_fields(self.page.content(), self.api)

    def fill(self, plan: list[Action]) -> list[str]:
        rejected = []
        for action in plan:
            try:
                ok = self._do(action)
            except Exception:  # a detached, hidden or changed element: leave it for review
                ok = False
            if not ok:
                rejected.append(action.dom_id)
            self._pause()
        return rejected

    def _do(self, a: Action) -> bool:
        box = self._box(a.dom_id)
        if a.kind == "type":
            box.fill(a.value)
        elif a.kind == "upload":
            box.set_input_files(a.value)
        elif a.kind == "paste":
            self.page.locator(f"[data-testid='{a.dom_id}-text']").click()
            self._box(f"{a.dom_id}_text").fill(a.value)
        elif a.kind == "check":
            box.check()
        elif a.kind == "choose":
            return self._choose(box, a.dom_id, a.value)
        return True

    def _choose(self, box, dom_id: str, value: str) -> bool:
        """Type into the React combobox, then click the option that means `value`."""
        options = self.page.locator(f"[id^='react-select-{dom_id}-option-']")
        box.click()
        box.fill(value.split(",")[0][:40])
        with contextlib.suppress(Exception):  # no options at all is handled below
            options.first.wait_for(timeout=6_000)
        texts = [t.strip() for t in options.all_inner_texts()]
        choice = pick_option(value, texts)
        if choice is None:
            box.fill("")
            box.press("Escape")
            return False
        options.nth(texts.index(choice)).click()
        return True

    def page_blockers(self) -> list[str]:
        challenges = self.page.locator(_CHALLENGES)
        visible = any(challenges.nth(i).is_visible() for i in range(challenges.count()))
        return ["CAPTCHA"] if visible else []

    def screenshot(self, path: Path) -> None:
        self.page.screenshot(path=path, full_page=True)

    def highlight(self, dom_ids: list[str]) -> None:
        """Outline fields for the owner at review."""
        self.page.evaluate(
            """ids => ids.forEach(id => {
                const el = document.getElementById(id);
                const wrap = '.field-wrapper, .select, .text-input-wrapper';
                const box = el && (el.closest(wrap) || el);
                if (box) box.style.outline = '3px solid #e11d48';
            })""",
            dom_ids,
        )
