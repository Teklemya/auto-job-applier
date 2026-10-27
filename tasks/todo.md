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
- [x] Workday password for isaac.teklemya@gmail.com in macOS Keychain (service `auto-job-applier-workday`)
- [x] Gmail 2-step verification turned on
- [x] `aja login` → sign into Jobright and Google once (2026-10-17)

## Phase 3: Real sites (supervised, shadow mode forced on)
- [x] T11 Jobright collector (moved up): browser profile + `aja login`, Liked + >80% match → final ATS URL → filters. `aja collect --source jobright`; live read 2026-10-17: 92 roles (26 Greenhouse/Lever/Workday pending). Liked page verified live with 3 liked jobs
- [ ] T7 Greenhouse engine + `aja review` (re-fill under 10s, highlight blockers, save to answer bank). Built 2026-10-17: `aja apply <id>` staged Lyft, Brex, SeatGeek live (drafts only). Left: owner runs `aja review` on one draft; work-auth option mapping (owner decision)
- [x] Greenhouse-first re-scope (2026-10-24): `aja run --limit N` (collect → screen → fit → apply the best
  Greenhouse jobs, then a summary with new fillable jobs per ATS) and the T10 submit path for Greenhouse
  were pulled ahead of T8/T9. T8 and T9 wait for a few days of supply numbers from `aja run`
- [x] Emailed security codes (2026-10-26): Greenhouse asks for an 8-character code after Submit.
  `inbox.py` reads it from Gmail over IMAP (app password in the Keychain via `aja login-mail`,
  read only, Greenhouse sender + company + after the click), and submits wait for their code one
  at a time. Left: first live run with the reader on
- [ ] T8 Lever engine. From the T7 review: pick the engine by ATS (CLI hard-codes Greenhouse) and add `highlight` to `FormSession`
- [ ] T9a Workday auth (D11): Google first, else Keychain password; `ats_accounts` table; verification wall → STAGED + notify, resend when stale
- [ ] T9b Workday wizard + native draft: reach Review; gate fail → Save for later

### Checkpoint C
- [ ] Owner reviews a week of shadow-mode drafts

## Phase 4: Autonomy
- [ ] T10 Submit path: confirm the success page before APPLIED, daily cap, screenshots. Built for
  Greenhouse (2026-10-24): APPLIED only on a confirmation page, a rejected submit or an emailed
  security code → STAGED, no confirmation → FAILED (never re-sent). Left: one live submit after the
  owner turns shadow mode off
- [x] T12 Scheduling + notifications: `aja schedule --at 21:30` (one launchd job, catch-up on wake), run lock, macOS notification, `aja last`
- [ ] T13 Salary lookup: Gemini + Search grounding range cache, $120k floor

### Checkpoint D
- [ ] First live week with a cap of 3 submits a day, then raise it
