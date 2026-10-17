# auto-job-applier

A personal pipeline that collects SWE roles (SimplifyJobs, Jobright), screens them, and fills
out applications on Workday, Greenhouse and Lever using Playwright. It submits only when
confident and saves everything else as a draft.

- Spec: `docs/SPEC.md`
- Confirmed intent: `docs/intent/auto-job-applier.md`
- Plan and tasks: `tasks/plan.md`, `tasks/todo.md`

## Commands
```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"   # setup
.venv/bin/playwright install chromium                          # browsers (Phase 3+)
.venv/bin/pytest -q                                            # tests
.venv/bin/ruff check . && .venv/bin/ruff format --check .      # lint / format
.venv/bin/aja --help                                           # CLI
```

## Layout
- `src/auto_job_applier/`: package (`cli.py` is the Typer entry point `aja`)
- `tests/`: pytest; `tests/fixtures/` holds saved real-world pages (README, ATS forms)
- Personal data lives in `$AJA_HOME` (default `~/.auto-job-applier/`): `profile.yaml`, the resume,
  `jobs.db`, the browser profile and artifacts. Tests set `AJA_HOME` to a temp dir automatically.

## Rules
- **Never change `settings.submit` (shadow mode) or raise `daily_submit_cap`.** Only the owner
  does that, by hand.
- Work-authorization and sponsorship answers come only from the profile, never from the LLM.
- Never guess a required form field. If it can't be answered confidently, stage a draft instead.
- Tests never hit live sites or the real Anthropic API. Use fixtures and stubs.
- Never commit personal data (profile, resume, DB).
- Git: one branch per phase (`phase-N-*`), one commit per task, PR into `main` at each
  checkpoint, regular merge (no squash).
