"""SimplifyJobs New-Grad README parser (R1).

The README is markdown with HTML tables. Each `## ` section (e.g. "💻 Software Engineering
New Grad Roles") holds an active table and a collapsed "Inactive roles" table. Rows have five
cells: company, role, location, application links, age. Conventions:
- company cell `↳` means "same company as the previous row"
- multi-location cells use <details><summary>N locations</summary>A</br>B</details>
- closed postings show 🔒 instead of an Apply link
- flags are emoji in the company/role text
"""

import urllib.request
from dataclasses import dataclass, field
from html.parser import HTMLParser

from ..models import Job
from .ats_detect import canonical_url, detect_ats

README_URL = "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/README.md"
SWE_SECTION = "Software Engineering"

FLAGS = {
    "🛂": "no_sponsorship",
    "🇺🇸": "us_citizenship",
    "🔥": "faang",
    "🎓": "advanced_degree",
}
CONTINUATION = "↳"


@dataclass
class _Cell:
    parts: list[str] = field(default_factory=list)
    links: list[tuple[str, str]] = field(default_factory=list)  # (href, img alt)

    @property
    def text(self) -> str:
        return " ".join("".join(self.parts).split())

    @property
    def lines(self) -> list[str]:
        return [" ".join(s.split()) for s in "".join(self.parts).split("\n") if s.strip()]


class _TableParser(HTMLParser):
    """Collects <td> cells per <tr>; ignores <summary> text ("4 locations")."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[_Cell]] = []
        self._row: list[_Cell] | None = None
        self._cell: _Cell | None = None
        self._href: str | None = None
        self._in_summary = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "tr":
            self._row = []
        elif tag == "td" and self._row is not None:
            self._cell = _Cell()
        elif tag == "summary":
            self._in_summary = True
        elif tag == "br" and self._cell:
            self._cell.parts.append("\n")
        elif tag == "a":
            self._href = a.get("href")
        elif tag == "img" and self._cell and self._href:
            self._cell.links.append((self._href, a.get("alt") or ""))

    def handle_endtag(self, tag):
        if tag == "td" and self._row is not None and self._cell:
            self._row.append(self._cell)
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None
        elif tag == "summary":
            self._in_summary = False
            if self._cell:
                self._cell.parts.append("\n")
        elif tag == "br" and self._cell:  # the README writes </br>
            self._cell.parts.append("\n")
        elif tag == "a":
            self._href = None

    def handle_data(self, data):
        if self._cell and not self._in_summary:
            self._cell.parts.append(data)


def _section(markdown: str, name: str) -> str:
    out, inside = [], False
    for line in markdown.splitlines():
        if line.startswith("## "):
            inside = name in line
            continue
        if inside:
            out.append(line)
    return "\n".join(out)


def _split_flags(text: str) -> tuple[str, list[str]]:
    flags = [flag for emoji, flag in FLAGS.items() if emoji in text]
    for emoji in FLAGS:
        text = text.replace(emoji, "")
    return " ".join(text.split()), flags


def parse_simplify(markdown: str, section: str = SWE_SECTION) -> list[Job]:
    parser = _TableParser()
    parser.feed(_section(markdown, section))
    jobs, company = [], ""
    for cells in parser.rows:
        if len(cells) != 5:
            continue
        company_cell, role_cell, location_cell, apply_cell, age_cell = cells
        if company_cell.text != CONTINUATION:
            company = company_cell.text
        href = next((h for h, alt in apply_cell.links if alt == "Apply"), None)
        if href is None:  # 🔒 closed
            continue
        company_name, company_flags = _split_flags(company)
        role, role_flags = _split_flags(role_cell.text)
        jobs.append(
            Job(
                url=canonical_url(href),
                company=company_name,
                role=role,
                location="; ".join(location_cell.lines),
                ats=detect_ats(href),
                flags=sorted(set(company_flags + role_flags), key=list(FLAGS.values()).index),
                age=age_cell.text,
                source="simplify",
            )
        )
    return jobs


def fetch_readme(url: str = README_URL, timeout: float = 30) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "auto-job-applier"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8")
