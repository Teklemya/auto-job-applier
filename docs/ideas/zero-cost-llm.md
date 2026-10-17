# Zero-cost LLM layer

Decided 2026-10-17. Recorded as D7 (revised) and D10 in `tasks/plan.md`.

## Problem Statement
How might we screen jobs and write application answers for $0, without weakening the
"never guess" submit rule?

## Recommended Direction
Use the LLM less, then send what's left to Gemini Flash on Google's free tier. Fit checks are
batched 8 jobs per call. The answer bank absorbs
repeat questions. Cover letters are written only when the field is required. What remains
(~20–40 calls/day at first, falling as the answer bank grows) goes to Gemini Flash on the free
tier through a provider interface in `llm.py` (`google-genai` SDK, JSON-schema structured
output). The `anthropic` dependency is dropped.

Running out of quota is treated like any other missing answer. The fit check waits and the job
stays PENDING; unresolved fields make the job a draft. Throughput drops, but the pipeline never
guesses and never pays.

## Free-tier budget (measured in AI Studio, 2026-10-17)
The first live run hit `limit: 20, model: gemini-3.8-flash` after 13 checks: the ~1,500/day
figure did not hold. Quotas are per model:

| Model | Free quota | Used for | Our setting |
|---|---|---|---|
| `gemini-3.5-flash-lite` | 500/day, 15/min | fit checks, 8 jobs per call | budget 450, 4.5s apart, minimal thinking |
| `gemini-3.8-flash` | 20/day, 5/min | form answers, cover letters | budget 18, 12.5s apart |

All limits are settings, not code. A 429 or 5xx gets one backoff retry, then counts as quota
exhausted.

## Key Assumptions to Validate
- [ ] Gemini Flash's `confident` flag is reliable. Test: in the shadow week, check every
      answer it marked confident
- [ ] The free quota holds about 40 calls/day. Test: log calls and 429s per run
- [ ] Flash writes cover letters that sound human. Test: Checkpoint B, 5 letters
- [x] ~~The keyword pre-filter doesn't drop good jobs.~~ It did (generic HPE/GlobalFoundries
      descriptions), and batching made it pointless: removed at Checkpoint B

## MVP Scope
In: `llm.py` provider interface + Gemini client, rate limiter + daily budget, batched fit
checks in T4, quota-exhausted → PENDING/draft handling, a call/429 counter in the run log.
Out: everything below.

## Not Doing (and Why)
- Paid Claude / Haiku / Batch API: the owner set a hard $0
- `claude -p` on the Pro plan: shares the owner's own limits, and Anthropic says unattended
  batches belong on API billing
- Local models (Ollama): weaker writing and uses laptop memory; reconsider if the free tier dies
- Multi-provider router + spend caps: more code than ~40 calls/day justifies
- Optional cover letters: low value, highest risk to the owner's name

## Open Questions
- T13 salary: is Gemini's Google Search grounding within free quota? Decide at T13
- Which Flash model id is on the free tier in October 2026? Check Google's docs at T4
