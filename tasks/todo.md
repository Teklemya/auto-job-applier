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
- [x] T4 JD fetch (Greenhouse/Lever/Workday public JSON) + Gemini Flash fit screen (llm.py provider + rate limiter + daily budget; batched 8/call; verdict strong/ok/stretch/mismatch, skip only `fit_skip` verdicts, score for ordering; quota out → stays PENDING; at most once per job). `aja fit`. Manual check done at Checkpoint B
- [x] T5 Answer resolver + D1 gate: profile → preferences → bank → LLM; sponsorship/auth always from profile (test-enforced)
- [x] T6 Answer writer: cover letters and "why us" grounded in resume + JD; `confident:false` when facts are missing

### Checkpoint B
- [x] Owner reads 5 generated cover letters / "why us" answers and approves the tone (2026-10-17)
- [x] Owner reviews fit verdicts for ~20 real jobs (every `mismatch` included) and confirms `fit_skip` (71 reviewed; citizenship rule added, keyword pre-filter removed)

## Owner prerequisites (before Phase 3)
- [ ] Resume PDF + filled `~/.auto-job-applier/profile.yaml`
- [x] Gemini API key (Google AI Studio, free tier), in the Keychain
- [ ] Workday password for isaac.teklemya@gmail.com in macOS Keychain (service `auto-job-applier-workday`)
- [ ] Gmail 2-step verification turned on
- [ ] `aja login` → sign into Jobright and Google once

## Phase 3: Real sites (supervised, shadow mode forced on)
- [ ] T11 Jobright collector (moved up): browser profile + `aja login`, Liked + >80% match → final ATS URL → filters
- [ ] T7 Greenhouse engine + `aja review` (re-fill under 10s, highlight blockers, save to answer bank)
- [ ] T8 Lever engine
- [ ] T9a Workday auth (D11): Google first, else Keychain password; `ats_accounts` table; verification wall → STAGED + notify, resend when stale
- [ ] T9b Workday wizard + native draft: reach Review; gate fail → Save for later

### Checkpoint C
- [ ] Owner reviews a week of shadow-mode drafts

## Phase 4: Autonomy
- [ ] T10 Submit path: confirm the success page before APPLIED, daily cap, screenshots
- [ ] T12 Scheduling + notifications: launchd at 8am and 2pm, catch-up on wake, lock, macOS summary
- [ ] T13 Salary lookup: Gemini + Search grounding range cache, $120k floor

### Checkpoint D
- [ ] First live week with a cap of 3 submits a day, then raise it
