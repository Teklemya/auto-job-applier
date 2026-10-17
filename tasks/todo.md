# auto-job-applier: Task List

Full details, acceptance criteria and decisions: `tasks/plan.md`. Intent: `docs/intent/auto-job-applier.md`.
Every task: `ruff check . && pytest` passes, plus its manual check.
**Shadow mode stays ON until the owner turns it off. No agent ever flips it.**

## Phase 1: Foundation (auto mode OK, no real sites)
- [x] T1 Scaffold + store + profile: `aja init`, profile validation errors, store transitions log events
- [x] T2 SimplifyJobs collector: HTML-table parser (SWE section, `↳`, 🔒, flags), canonical URLs, ATS detection, idempotent ingest
- [x] T3 Hard filters + dedupe: 🛂/🇺🇸/🎓/senior+ → SKIPPED(reason), location priority (remote > KC > any company in US hubs), cross-source dedupe

### Checkpoint A
- [ ] Tests pass. `aja collect` on the live README; owner reviews the queue and SKIPPED reasons

## Phase 2: Brains (auto mode OK, stubbed LLM in tests)
- [ ] T4 JD fetch (Greenhouse/Lever/Workday public JSON) + Sonnet 5.5 fit screen (verdict strong/ok/stretch/mismatch, skip only `fit_skip` verdicts, score for ordering; cached resume, at most once per job)
- [ ] T5 Answer resolver + D1 gate: profile → preferences → bank → LLM; sponsorship/auth always from profile (test-enforced)
- [ ] T6 Answer writer: cover letters and "why us" grounded in resume + JD; `confident:false` when facts are missing

### Checkpoint B
- [ ] Owner reads 5 generated cover letters / "why us" answers and approves the tone
- [ ] Owner reviews fit verdicts for ~20 real jobs (every `mismatch` included) and confirms `fit_skip`

## Owner prerequisites (before Phase 3)
- [ ] Resume PDF + filled `~/.auto-job-applier/profile.yaml`
- [ ] Anthropic API key
- [ ] Workday email + password in macOS Keychain
- [ ] `aja login` → sign into Jobright once

## Phase 3: Real sites (supervised, shadow mode forced on)
- [ ] T11 Jobright collector (moved up): browser profile + `aja login`, Liked + >80% match → final ATS URL → filters
- [ ] T7 Greenhouse engine + `aja review` (re-fill under 10s, highlight blockers, save to answer bank)
- [ ] T8 Lever engine
- [ ] T9a Workday auth: sign in or create the account (Keychain); verification wall → STAGED
- [ ] T9b Workday wizard + native draft: reach Review; gate fail → Save for later

### Checkpoint C
- [ ] Owner reviews a week of shadow-mode drafts

## Phase 4: Autonomy
- [ ] T10 Submit path: confirm the success page before APPLIED, daily cap, screenshots
- [ ] T12 Scheduling + notifications: launchd at 8am and 2pm, catch-up on wake, lock, macOS summary
- [ ] T13 Salary lookup: `web_search` range cache, $120k floor

### Checkpoint D
- [ ] First live week with a cap of 3 submits a day, then raise it
