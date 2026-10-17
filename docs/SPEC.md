# Spec: auto-job-applier

Assembled from `docs/intent/auto-job-applier.md` (confirmed intent) and `tasks/plan.md` (approved
plan). It adds no new requirements. If the three disagree, the intent wins, then this spec.

## Objective
A personal, local pipeline that collects SWE roles that fit its single owner, fills out the
applications, **submits the ones it is confident about**, and saves the rest as drafts for a short
evening review. Target: about 10 applications a day (50+ a week). The owner works full-time and wants
almost no screen time spent applying. The goal is interview flow, for practice and career mobility.

## Owner facts that shape behavior
- On OPT and authorized to work in the US. **Sponsorship now or in the future: No** (green card
  via spouse).
- Relocating to the Kansas City metro in about 6 months.

## Functional requirements

### Collect
- **R1** SimplifyJobs New-Grad README, Software Engineering section. The table is **HTML**
  (active + "Inactive roles" tables). `↳` = same company as the previous row. 🔒 = closed, drop
  it. Capture the flags 🛂 🇺🇸 🎓 🔥.
- **R2** Jobright: Liked jobs plus jobs with a match above 80%, read through the tool's
  persistent, logged-in browser profile (like a human visit), twice a day. No token scraping.
- **R3** Canonicalize URLs (strip `utm_*`/`ref`) and detect the ATS by domain: Workday,
  Greenhouse, Lever, otherwise MANUAL.

### Screen
- **R4** Hard skip: senior and above, 🛂, 🇺🇸, 🎓 (owner has a bachelor's only).
- **R5** Titles: new-grad and early-career SWE (SWE I/II, Backend, Full-stack, New Grad).
- **R6** Location priority: remote > KC metro on-site/hybrid > **any company** in a major US
  tech hub (owner-editable city list) > bare "United States" (location unknown). Anything else,
  including non-US, is skipped.
- **R7** Never apply to the same company + role twice, including across sources. A fillable
  posting takes the role over from an unsupported-site (MANUAL) copy.
- **R8** LLM fit check (resume keywords vs. job description) with a verdict
  (strong/ok/stretch/mismatch), a score and a reason. Only verdicts in `fit_skip` (default
  `mismatch`) → skip; the score only orders the queue (D9).
- **R9** Skipped and MANUAL jobs are recorded with a reason, never silently dropped. Jobs not yet
  in flight are re-screened when their flags or location change, and postings that vanish from
  the source (closed) are retired as SKIPPED. An empty parse is an error, never "all closed".

### Answer
- **R10** Resolution order for every form field: profile → preferences → answer bank → LLM.
  Every answer records its source.
- **R11** Work authorization and sponsorship answers come **only** from the profile, never from
  the LLM.
- **R12** Preferences are set once: EEO, start date, "how did you hear", etc. Salary is left blank
  unless required. When required, use a market range from a web search (levels.fyi, Reddit), never
  below $120k.
- **R13** Cover letters and open-ended answers are written by the LLM from the resume and the job
  description, tailored and human-sounding. The LLM flags `confident:false` when it would need
  facts that aren't in the resume.

### Submit gate (D1)
- **R14** Submit only if **every required field** has a confident answer per R10–R13, the page
  shows no validation errors, and there is no CAPTCHA. Otherwise save a **draft** and notify.
  **Never guess** a required field.
- **R15** The gate is deterministic code over the resolved answers, not an LLM opinion.

### Drafts and review
- **R16** Workday: use Workday's native saved application (stop before Submit).
- **R17** Greenhouse/Lever: store the answer packet. `aja review` re-fills the form in under 10
  seconds, highlights the blocking fields, and the owner submits. Answers given at review go into
  the answer bank.
- **R18** The review list covers drafts plus MANUAL links for unsupported sites.

### Run and notify
- **R19** Runs on the owner's MacBook: launchd at 8am and 2pm, catches up on wake, and a lock
  prevents overlapping runs. The code stays portable for a future home server.
- **R20** Each run ends with a macOS notification summary (submitted / drafts / manual).

## Safety requirements
- **S1** Shadow mode is on by default: everything is drafted and nothing is submitted. **Only the
  owner** turns it off by editing the config. No agent or automated task may change it.
- **S2** A daily submit cap (default 15; 3 for the first live week).
- **S3** Mark APPLIED only after the confirmation page is detected. Save a screenshot and the
  answer packet for every submit and every draft.
- **S4** Personal data (profile, resume, DB, browser profile) lives in `~/.auto-job-applier/`
  and never in the repo. Secrets go in the macOS Keychain or environment variables.

## Technical decisions
- Python ≥3.12, Typer CLI (`aja`), Playwright (sync, persistent context, headed real Chrome),
  Pydantic + YAML profile, SQLite (stdlib).
- LLM: Claude Sonnet 5.5 (`claude-sonnet-5-5`) via the Anthropic SDK, with structured outputs
  and the resume/profile in a cached system prompt. Salary uses the `web_search` server tool.
- Engines handle navigation only. They extract `FormField`s, and a shared resolver answers them.
- Module layout, statuses and task breakdown are in `tasks/plan.md`.

## Out of scope (v1)
- Teaching new portals by demonstration
- Ashby, iCIMS, Oracle, SmartRecruiters and custom career sites (these are MANUAL)
- On-site roles outside KC and the hub cities
- Interview scheduling and follow-ups
- Any dashboard beyond the review list
- GitHub Actions and cloud hosting

## Acceptance (v1 done)
- A scheduled run collects, screens and processes jobs from both sources without the owner.
- A shadow-mode week produces drafts the owner judges correct.
- After that, live runs average about 10 applications a day (submitted + drafts), and the owner's
  only involvement is the review list.
- `ruff check . && pytest` pass.
