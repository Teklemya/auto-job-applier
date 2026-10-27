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
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path

from ..answers.bank import normalize, same_option
from ..geo import US_STATES
from ..jd_fetch import api_url, get_json
from ..models import FormField, Job
from .base import REJECTED, Action, PageField, SubmitResult

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
        # A question shown as a set of boxes ("How did you hear about us? Click all that
        # apply"): one field, its legend the label, each box an option.
        self._group: dict | None = None

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
        elif tag == "fieldset" and a.get("id"):
            self._group = {
                "id": a["id"],
                "kind": "checkboxes",
                "required": a.get("aria-required") == "true",
                "section": self._section[0] if self._section else None,
                "option_ids": [],
            }
        elif tag == "legend" and self._group:
            self._start_label(self._group["id"], "legend")
        elif tag == "label" and a.get("for"):
            self._start_label(a["for"], "label")
        elif tag == "input" and self._in_group(a):
            self._group["option_ids"].append(a["id"])
        elif tag in ("input", "textarea") and a.get("id"):
            self._add_input(tag, a)

    def _in_group(self, a: dict) -> bool:
        return bool(
            self._group
            and a.get("type") in ("checkbox", "radio")
            and a.get("id", "").startswith(self._group["id"] + "_")
        )

    def handle_endtag(self, tag):
        if self._label_for is not None and tag == self._label_tag:
            self._end_label()
        if tag == "fieldset" and self._group:
            if self._group["option_ids"]:
                self.inputs.append(self._group)
            self._group = None
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
        choices = options.get(dom_id, [])
        if i["kind"] == "checkboxes":
            choices = [parser.labels.get(o, "") for o in i["option_ids"]]
        if i["section"]:
            label = f"{i['section']}: {label}"
        field = FormField(
            label=html_lib.unescape(label),
            kind=i["kind"],
            required=required,
            options=choices,
        )
        fields.append(PageField(dom_id, field))
    return fields


def pick_option(value: str, options: list[str]) -> str | None:
    """The dropdown option meaning `value`, or None when no single option clearly fits.

    Places come back spelled out ("Kansas City, Missouri, United States"), so a value like
    "Kansas City, MO" matches on the city and then on the state, by name or abbreviation.
    """
    if same := same_option(value, options):
        return same
    head, *rest = [normalize(part) for part in value.split(",")]
    hits = [o for o in options if normalize(o).startswith(head)]
    for part in rest:
        names = {part, normalize(US_STATES.get(part.upper(), part))}
        narrowed = [o for o in hits if names & set(normalize(p) for p in o.split(","))]
        hits = narrowed or hits
    return hits[0] if len(hits) == 1 else None


def fetch_questions(job: Job, get: Callable[[str], dict] = get_json) -> dict:
    return get(api_url(job) + "?questions=true")


CODE_ASKED = "email security code: the application is not sent until it is entered"


def submit_outcome(
    url: str, body_text: str, errors: list[str], form_showing: bool, code_asked: bool = False
) -> SubmitResult | None:
    """What the page shows after Submit, or None while it is still deciding.

    Greenhouse moves to a /confirmation page or replaces the form with a thank-you. The job
    description above the form can say "after your application is submitted", so thank-you
    text counts only once the form is gone. Some boards email a security code first: the
    application isn't sent until it is entered, so that is a draft for the owner, as are
    validation errors. `errors` and `code_asked` only count what appeared after the click.
    """
    if "/confirmation" in url or (not form_showing and _CONFIRMED.search(body_text)):
        return SubmitResult(confirmed=True)
    if code_asked:
        return SubmitResult(False, [CODE_ASKED])
    if errors:
        return SubmitResult(False, [f"{REJECTED}{e}" for e in errors])
    return None


# --- the live page ----------------------------------------------------------------------------

FORM_READY = "#first_name"
_CHALLENGES = "iframe[src*='recaptcha'][src*='bframe'], iframe[src*='hcaptcha'][src*='challenge']"
SUBMIT_BUTTON = "button[type='submit']"
_ERRORS = ".helper-text--error, .error-message, [role='alert']"
_CONFIRMED = re.compile(
    r"thank you for (applying|your application)|application (has been )?(submitted|received)"
    r"|we(.ve| have) received your application",
    re.I,
)
# "A verification code was sent to you@x.com. ... enter the 8-character code" + "Security code"
_SECURITY_CODE = re.compile(r"(security|verification) code|enter the \d+.character code", re.I)
SUBMIT_WAIT_MS = 30_000
# Required inputs still empty: unticked boxes, radio groups with nothing picked, blank text, and
# dropdowns still showing their placeholder (the choice sits beside the input, not in it). File
# inputs are left to the field reader, which knows about the paste-text alternative.
_MISSING_REQUIRED_JS = """() => {
  const tidy = t => (t || '').replace(/\\s+/g, ' ').replace(/\\*\\s*$/, '').trim();
  const label = el => {
    const byFor = el.id && document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
    let text = tidy((byFor && byFor.innerText) || el.getAttribute('aria-label')
      || (el.closest('label, fieldset, .checkbox, .field-wrapper') || {}).innerText || el.id);
    if (['checkbox', 'radio'].includes((el.type || '').toLowerCase())) {
      // "I agree" means nothing alone: the question above it says what is agreed to.
      const legend = el.closest('fieldset') && el.closest('fieldset').querySelector('legend');
      const wrapper = el.closest('.field-wrapper');
      const question = tidy((legend && legend.innerText) || (wrapper && wrapper.innerText));
      if (question && !question.includes(text)) text = `${question} ${text}`;
      else if (question) text = question;
    }
    return text.slice(0, 400);
  };
  const shown = el => {
    const box = el.closest('label, fieldset, .checkbox, .field-wrapper, .select') || el;
    return box.getClientRects().length > 0 && !el.disabled;
  };
  const out = [], seenRadio = new Set();
  const form = document.querySelector('#application-form, form') || document;
  for (const el of form.querySelectorAll('input, textarea, select')) {
    const required = el.required || el.getAttribute('aria-required') === 'true';
    const type = (el.type || '').toLowerCase();
    if (!required || ['hidden', 'file', 'search', 'submit', 'button'].includes(type)) continue;
    if (el.getAttribute('aria-hidden') === 'true') continue;  // a dropdown's stand-in input
    if (!shown(el)) continue;
    let empty;
    if (type === 'checkbox') empty = !el.checked;
    else if (type === 'radio') {
      if (seenRadio.has(el.name)) continue;
      seenRadio.add(el.name);
      empty = !form.querySelector(`input[type=radio][name="${CSS.escape(el.name)}"]:checked`);
    } else if (el.getAttribute('role') === 'combobox') {
      // React Select shows a placeholder, and keeps a hidden required input, only while empty.
      const shell = el.closest('.select-shell') || el.parentElement;
      empty = !!shell.querySelector('input[aria-hidden="true"][required], .select__placeholder')
        && !shell.querySelector('.select__single-value, .select__multi-value');
    } else empty = !el.value.trim();
    const kind = ['checkbox', 'radio'].includes(type) ? type
      : el.getAttribute('role') === 'combobox' ? 'select' : 'text';
    if (empty) out.push({id: el.id || el.name || '', label: label(el), kind});
  }
  for (const fs of form.querySelectorAll('fieldset[aria-required="true"]')) {
    if (!fs.getClientRects().length || fs.querySelector('input:checked')) continue;
    const legend = fs.querySelector('legend');
    const text = ((legend && legend.innerText) || fs.id).replace(/\\s+/g, ' ');
    out.push({id: fs.id, label: text.replace(/\\*\\s*$/, '').trim().slice(0, 160),
              kind: 'checkboxes'});
  }
  return out;
}"""


def code_newly_asked(before: str, after: str) -> bool:
    """A security-code prompt appeared after the click. A job description that mentions
    "security code" must not hide it, so mentions are counted, not just looked for."""
    return len(_SECURITY_CODE.findall(after)) > len(_SECURITY_CODE.findall(before))


class GreenhouseSession:
    """The application form open in the tool's browser (see apply.FormSession)."""

    def __init__(self, page, api: dict | None, company: str = ""):
        self.page, self.api, self.company = page, api, company
        self.picked: dict[str, str] = {}  # dom id → the option chosen in a type-to-search box
        self.offered: dict[str, list[str]] = {}  # dom id → options seen there while typing
        # How long to wait for the owner to type an emailed security code; 0 = unattended.
        self.code_wait_ms = 0
        # Reads the emailed code for (company, Submit click time); None = no inbox access.
        self.codes: Callable[[str, datetime], str | None] | None = None

    @classmethod
    def open(cls, page, job: Job, api: dict | None) -> "GreenhouseSession":
        page.goto(job.url, wait_until="domcontentloaded")
        # No form (a closed posting or a changed page) leaves fields() empty → NO_FORM.
        with contextlib.suppress(Exception):
            page.wait_for_selector(FORM_READY, timeout=30_000)
        return cls(page, api, job.company)

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
        elif a.kind == "tick_option":
            return self._tick_option(a.dom_id, a.value)
        elif a.kind == "choose":
            return self._choose(box, a.dom_id, [a.value, *a.alternatives])
        return True

    def _tick_option(self, group_id: str, option: str) -> bool:
        """Tick the one box in a group whose label is `option`."""
        labels = self._box(group_id).locator("label")
        texts = [" ".join(t.split()) for t in labels.all_inner_texts()]
        if texts.count(option) != 1:
            return False
        target = labels.nth(texts.index(option)).get_attribute("for")
        return bool(target) and self.tick(target)

    def _options_for(self, box, dom_id: str, query: str):
        """Type `query` into the React combobox and return (option locator, option texts)."""
        options = self.page.locator(f"[id^='react-select-{dom_id}-option-']")
        box.click()
        box.fill(query)
        with contextlib.suppress(Exception):  # no options at all is handled by the caller
            options.first.wait_for(timeout=6_000)
        texts = [t.strip() for t in options.all_inner_texts()]
        seen = self.offered.setdefault(dom_id, [])
        seen += [t for t in texts if t and t not in seen]
        return options, texts

    def _choose(self, box, dom_id: str, values: list[str]) -> bool:
        """Click the option meaning the first of `values` the box offers, the way a person
        works down their own list. If none fits, the options seen stay in `offered` (plus the
        list an empty box shows) so the owner can pick one at review."""
        for value in values:
            options, texts = self._options_for(box, dom_id, value.split(",")[0][:40])
            choice = pick_option(value, texts)
            if choice is not None:
                options.nth(texts.index(choice)).click()
                self.picked[dom_id] = choice
                return True
        self._options_for(box, dom_id, "")
        box.fill("")
        box.press("Escape")
        return False

    def page_blockers(self) -> list[str]:
        challenges = self.page.locator(_CHALLENGES)
        visible = any(challenges.nth(i).is_visible() for i in range(challenges.count()))
        return ["CAPTCHA"] if visible else []

    def screenshot(self, path: Path) -> None:
        self.page.screenshot(path=path, full_page=True)

    def missing_required(self) -> list[tuple[str, str, str]]:
        found = self.page.evaluate(_MISSING_REQUIRED_JS)
        return [(m["id"], m["label"], m["kind"]) for m in found]

    def tick(self, dom_id: str) -> bool:
        """Tick a box. A styled box hides the real input, so its label is clicked instead."""
        box = self._box(dom_id)
        try:
            box.check(timeout=3_000)
        except Exception:
            with contextlib.suppress(Exception):
                self.page.locator(f"label[for='{dom_id}']").first.click(timeout=3_000)
        try:
            return box.is_checked()
        except Exception:
            return False

    def wait_for_owner(self, wait_ms: int) -> SubmitResult:
        """Watch the page while the owner finishes it and clicks Submit (and types any
        emailed code). Unsent when the form is still there at the end."""
        for _ in range(max(1, wait_ms // 1_000)):
            self.page.wait_for_timeout(1_000)
            with contextlib.suppress(Exception):  # the page is navigating
                showing = self.page.locator(FORM_READY).count() > 0
                outcome = submit_outcome(self.page.url, self.page.inner_text("body"), [], showing)
                if outcome is not None and outcome.confirmed:
                    return outcome
        with contextlib.suppress(Exception):
            if self.page.locator(FORM_READY).count() > 0:
                return SubmitResult(confirmed=False)
        return SubmitResult(False, ["the form is gone but no confirmation page was seen"])

    def _errors(self) -> list[str]:
        return [t.strip() for t in self.page.locator(_ERRORS).all_inner_texts() if t.strip()]

    def submit(self) -> SubmitResult:
        """Click Submit and watch the page. When an emailed security code is asked for, read
        it from the inbox (`codes`) and type it; failing that, keep the page open
        `code_wait_ms` for the owner to type it."""
        before = set(self._errors())  # banners and notices already on the page aren't errors
        body_before = self.page.inner_text("body")
        clicked_at = datetime.now(UTC)
        self.page.locator(SUBMIT_BUTTON).last.click()
        ticks, code_handled = SUBMIT_WAIT_MS // 500, False
        while ticks > 0:
            ticks -= 1
            self.page.wait_for_timeout(500)
            with contextlib.suppress(Exception):  # the page is navigating
                new = [e for e in self._errors() if e not in before]
                showing = self.page.locator(FORM_READY).count() > 0
                body = self.page.inner_text("body")
                code = code_newly_asked(body_before, body)
                outcome = submit_outcome(self.page.url, body, new, showing, code)
                if outcome is None:
                    continue
                if outcome.problems != [CODE_ASKED] or code_handled:
                    if outcome.problems == [CODE_ASKED]:
                        continue  # still on the code page: wait out the time given
                    return outcome
                code_handled = True
                if self.codes and (emailed := self.codes(self.company, clicked_at)):
                    self._enter_code(emailed)
                    ticks = SUBMIT_WAIT_MS // 500
                elif self.code_wait_ms:
                    ticks = self.code_wait_ms // 500
                else:
                    return outcome
        if code_handled:
            return SubmitResult(False, [CODE_ASKED])
        if self.page_blockers():  # a challenge after the click: nothing was sent yet
            return SubmitResult(False, ["CAPTCHA after Submit: solve it at review"])
        return SubmitResult(confirmed=False)

    def _enter_code(self, code: str) -> None:
        """Type the code into Greenhouse's one-character boxes (or a single field), then
        submit again if the button is still there."""
        boxes = self.page.locator("input[maxlength='1']:visible")
        if boxes.count() >= len(code):
            boxes.first.click()
            self.page.keyboard.type(code, delay=80)
        else:
            self.page.get_by_label(re.compile("security code", re.I)).first.fill(code)
        with contextlib.suppress(Exception):  # some boards submit as soon as the code is full
            self.page.locator(SUBMIT_BUTTON).last.click(timeout=3_000)

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
