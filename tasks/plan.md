# auto-job-applier: Spec & Plan

## Context
The owner works full-time and has no energy to apply to jobs after work. They want a personal
pipeline that runs without them: it collects SWE roles that fit, fills out applications, submits
the ones it is confident about, and saves the rest as drafts for a short evening review. The
target is about 10 applications a day (50+ a week), for interview practice and career mobility.
Source of truth: `docs/intent/auto-job-applier.md` (confirmed 2026-10-17). The repo is empty
except for that doc.

## Decisions log
| # | Topic | Decision |
|---|-------|----------|
| D1 | Submit rule | Submit only if **every required field** is answered from the profile, the preferences, the answer bank, or a confident LLM answer, the page shows no validation errors, and there is no CAPTCHA. Otherwise save a draft and notify. Never guess. |
| D2 | Workday drafts | Workday's own saved application in the tenant account. The tool stops before Submit. |
| D3 | Greenhouse/Lever drafts | **Answer packet + re-fill.** Resolved answers are stored in the DB. `aja review` re-fills the form in seconds and highlights the blocking fields, then the owner submits. Answers given at review go into the answer bank. |
| D4 | CAPTCHA | A visible challenge forces a draft. The owner solves it at review. |
| D5 | Where it runs | Laptop for v1. launchd fires at 8am and 2pm, and a missed run catches up on wake. The code stays portable for a future home server. No GitHub Actions in v1. |
| D6 | Salary (required only) | Claude's `web_search` server tool → company and role range (levels.fyi, Reddit), cached per company+level. Answer at the upper-middle of the range with a $120k floor. |
| D7 | LLM | Claude Sonnet 5.5 (`claude-sonnet-5-5`). The resume and profile go in a cached system prompt, and structured outputs are used for every decision. About $7–10 a week. |
| D8 | Jobright | Read it in the same persistent browser profile, like a human visit (Liked list + >80% match), twice a day. No token scraping. |
| D9 | Fit rule | The LLM returns a verdict (`strong` / `ok` / `stretch` / `mismatch`) plus a 0–100 score, matched keywords and a reason. Only verdicts in `settings.fit_skip` (default `[mismatch]`) → SKIPPED(fit). The score only orders the queue and never decides a skip. Calibrated at Checkpoint B on ~20 real jobs. |

## Architecture

### Pipeline
```
collect ──► screen ──► queue ──► apply ─────────────────────────► notify ──► review (owner)
SimplifyJobs  hard filters  SQLite   open → extract fields → resolve answers   macOS        drafts + manual links
Jobright      dedupe                 → fill → gate(D1) → submit | draft        summary      re-fill, owner submits
              LLM fit check
```

### Key design idea: ATS-agnostic answering
Engines handle only navigation. Each engine extracts the visible form into a common list of
`FormField(label, kind, required, options)` items. A single **AnswerResolver** decides every
value in this order:
1. profile mapping
2. preferences
3. answer bank (questions answered before, fuzzy-matched)
4. LLM

The LLM returns `{value, confident, reason}` via structured output. The submit gate (D1) is
deterministic code over the resolved packet, not an LLM opinion. As a result, Greenhouse, Lever
and Workday share all the answering logic, and v2's "teach a new portal" only needs navigation.

### Modules (`src/auto_job_applier/`)
| Module | Responsibility |
|---|---|
| `cli.py` | Typer entry point `aja`: `init`, `collect`, `run`, `review`, `list`, `stats`, `mark`, `login` |
| `store.py` | SQLite (stdlib). Tables: `jobs`, `answer_packets`, `answer_bank`, `salary_cache`, `events` (audit log) |
| `models.py` | `Job`, `Status`, `ATS`, `FormField`, `ResolvedAnswer` |
| `profile.py` | Pydantic schema + YAML loader: facts, preferences, EEO, resume path, location targeting |
| `sources/simplify.py` | Parse the **HTML** tables in the SWE section. Handle `↳` (same company as the previous row) and 🔒 (closed). Take the flags 🛂 🇺🇸 🎓 🔥 from the company and role cells, and strip `utm_*`/`ref` from links. |
| `sources/jobright.py` | Browser read of the Liked and recommended pages, and resolve each link to its final ATS URL |
| `sources/ats_detect.py` | Domain → ATS (Workday, Greenhouse, Lever, else MANUAL) |
| `screen.py` | Hard filters (🛂, 🇺🇸, senior+ regex, 🎓, location priority: remote > KC metro > any company in a US tech hub), cross-source dedupe on canonical URL and company+normalized title. Then fetch the job description and run the LLM fit check. |
| `jd_fetch.py` | Job description text from public endpoints: Greenhouse boards API, Lever postings API, Workday `cxs` JSON. No browser needed. |
| `answers/resolver.py` | The resolution order above, plus the D1 gate |
| `answers/writer.py` | LLM-written cover letters and "why us" answers from the resume and the JD, in a human tone |
| `answers/salary.py` | D6 |
| `llm.py` | Anthropic SDK client and prompts, with Sonnet 5.5 and prompt caching |
| `engines/base.py` | `ATSEngine`: `open`, `extract_fields`, `fill`, `blockers`, `submit`, `save_draft` |
| `engines/greenhouse.py`, `engines/lever.py` | Single-page forms |
| `engines/workday.py` | `data-automation-id` selectors kept in one table. Sign in or create the tenant account (password in macOS Keychain). Then loop: extract → resolve → fill → Next until Review, then gate → Submit or "Save and continue later". Email verification forces a draft. |
| `browser.py` | Playwright persistent context (dedicated profile dir, real Chrome channel, headed), with randomized human pacing |
| `notify.py` | macOS notification plus a run summary ("6 submitted, 3 drafts, 2 manual") |

### Statuses
`NEW → SKIPPED(reason) | PENDING → APPLIED | STAGED(draft) | FAILED | MANUAL(unsupported site)`.
These extend the kickoff's four with SKIPPED and MANUAL so filtered and unsupported jobs show up
in review instead of disappearing.

### Safety rails
- **Shadow mode, on by default for the first week:** everything is drafted and nothing is
  submitted. The owner audits what *would* have been submitted, then flips `submit: true`.
- A daily submit cap (default 15) and one company+role ever (D-dedupe).
- A lock file so the 8am and 2pm runs can't overlap. A screenshot and the answer packet are
  saved for every submit and every draft.
- Personal data lives in `~/.auto-job-applier/` and never in the repo.

## Owner prerequisites (needed before Phase 3; Phases 1–2 don't need them)
- Resume PDF and a filled-in `~/.auto-job-applier/profile.yaml`
- Anthropic API key (`ANTHROPIC_API_KEY` or `ant auth login`)
- A single Workday email + password, stored in the macOS Keychain
- One-time `aja login` to sign into Jobright in the tool's browser profile

## Task breakdown
On approval this is written to `tasks/plan.md` + `tasks/todo.md`. Verification for every task
is `ruff check . && pytest`, plus the manual check listed.

### Phase 1: Foundation (auto mode OK, no real sites)
**T1 Scaffold + store + profile** (M). Files: `pyproject.toml`, `CLAUDE.md`, `models.py`,
`store.py`, `profile.py`, `profile.example.yaml`, `cli.py`
- `aja init` creates `~/.auto-job-applier/` and copies the example profile
- Invalid profile YAML (missing email, nonexistent resume path) produces a clear validation error
- The store round-trips jobs and every status transition writes an `events` row
- *Manual:* `aja init && aja stats` runs clean

**T2 SimplifyJobs collector** (M). Files: `sources/simplify.py`, `sources/ats_detect.py`,
`tests/fixtures/simplify_readme.html`
- Parses only the SWE section (active + inactive tables). `↳` inherits the company, 🔒 rows are
  dropped, and 🛂 🇺🇸 🎓 🔥 are captured as flags
- URLs are canonicalized (utm/ref stripped), and the ATS is detected for Workday, Greenhouse,
  Lever and MANUAL
- Re-running ingest creates no duplicates and never resets a job's status
- *Manual:* `aja collect` on the live README produces a sensible job count by ATS

**T3 Hard filters + dedupe** (S). Files: `screen.py`
- 🛂, 🇺🇸, 🎓 and senior+ titles → SKIPPED with a reason
- Location priority: remote > KC metro (Olathe, Overland Park, etc.) > any company in a US tech
  hub > bare "United States", else SKIPPED
- Cross-source dedupe on canonical URL or company + normalized title
- *Manual:* spot-check `aja list --status SKIPPED` reasons for 20 rows

### Checkpoint A: review the queue by eye with the owner

### Phase 2: Brains (auto mode OK, stubbed LLM in tests)
**T4 Job description fetch + LLM fit screen** (M). Files: `jd_fetch.py`, `llm.py`, `screen.py`
- Job description text from the Greenhouse, Lever and Workday public JSON endpoints
- Fit result `{verdict, score, matched_keywords, reason}` via structured output on Sonnet 5.5,
  with the resume in a cached system prompt. The prompt defines each verdict (D9)
- Verdict in `settings.fit_skip` (default `[mismatch]`) → SKIPPED(fit). The score only orders the
  queue. The fit check runs at most once per job
- *Manual:* fit reasons for 10 real jobs read correctly

**T5 Answer resolver + gate** (M). Files: `answers/resolver.py`, `answers/bank.py`, `models.py`
- Resolution order: profile → preferences → bank → LLM. Every answer records its source
- Gate: any required field without a confident answer → draft, with the blocking fields listed
- Sponsorship and authorization fields always come from the profile, never the LLM
  (test-enforced)

**T6 Answer writer** (S). Files: `answers/writer.py`
- Cover letters and "why us" answers are grounded in the resume and the job description, within
  a length limit
- The LLM marks `confident:false` when it would need facts that aren't in the resume

### Checkpoint B: the owner reads 5 generated cover letters and "why us" answers and approves the tone
The owner also reviews fit verdicts and reasons for ~20 real jobs, every `mismatch` included, and
adjusts `fit_skip` if needed (D9).

### Phase 3: Real sites (supervised, shadow mode forced on)
**T11 Jobright collector** (M), moved up at Checkpoint A because Jobright is the main source of
fresh postings. Files: `browser.py`, `sources/jobright.py`. Persistent browser profile with
`aja login`. Read the Liked and recommended (>80%) pages, resolve each to its final ATS URL, and
feed into T3. *Manual:* one live read whose jobs appear in `aja list` with correct ATS.

**T7 Greenhouse engine + `aja review`** (M). Files: `engines/base.py`, `engines/greenhouse.py`, `cli.py`
- Extracts `FormField`s from saved HTML fixtures and a live form, fills them, uploads the resume
- A visible CAPTCHA, validation errors, or the gate failing → STAGED with a stored packet
- `aja review` re-fills a STAGED job in under 10 seconds, highlights the blocking fields, and
  saves new answers to the bank
- *Manual:* 3 live Greenhouse jobs staged, re-filled and inspected

**T8 Lever engine** (S). Same acceptance shape as T7.

**T9a Workday auth** (M). Sign in, or create the tenant account using Keychain credentials. An
email-verification wall → STAGED with a note. Selectors live in one table.

**T9b Workday wizard + native draft** (M). Loop through extract → resolve → fill → Next until
Review. On gate failure, "Save and continue later". *Manual:* 2 live tenants reach Review.

### Checkpoint C: the owner reviews a week of shadow-mode drafts

### Phase 4: Autonomy
**T10 Submit path** (S). Gate pass + `submit: true` → submit, then confirm the success page
before marking APPLIED. Daily cap and a screenshot per action. Shadow mode can only be turned off
by the owner editing the config.

**T12 Scheduling + notifications** (S). Files: `notify.py`, `launchd/*.plist`, `aja schedule`.
Runs at 8am and 2pm and catches up on wake, a lock file prevents overlap, and each run ends
with a macOS summary notification.

**T13 Salary lookup** (S). `web_search` → range cached per company+level, never below the $120k
floor.

### Checkpoint D: first live week with a cap of 3 submits a day, then raise it

## Risks
| Risk | Impact | Mitigation |
|---|---|---|
| Workday selectors differ per tenant or change | High | Selector table + fixture tests. Unknown page → STAGED, never guessed |
| Bot detection or CAPTCHAs | Med | Headed real Chrome, residential IP, human pacing, low volume. CAPTCHA → draft |
| Bad answer submitted under the owner's name | High | Deterministic gate, sponsorship answers hard-coded, shadow week, daily cap |
| Jobright layout change | Low | Isolated reader. SimplifyJobs keeps working |

## Verification
- **Unit:**
  - The SimplifyJobs parser against a saved copy of the real README as a fixture (`↳`, 🔒,
    flags, the inactive table)
  - Each filter rule and dedupe
  - The resolver and gate with a stubbed LLM: one missing required field → draft
  - Store transitions
- **Engines:** Playwright tests against saved HTML snapshots of real Greenhouse, Lever and
  Workday forms served locally. They assert which fields are extracted and filled, and that
  nothing is submitted when the gate fails.
- **End-to-end:**
  - `aja collect` on the live README, then check the queue by eye
  - `aja run --limit 3` in shadow mode on real postings, then inspect the drafts and answer
    packets
  - Only then enable submitting with the cap set to 3 a day for the first live week
- `ruff check` and `pytest` pass on every slice.
