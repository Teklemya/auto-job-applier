# Intent: auto-job-applier

Status: **Confirmed** (2026-10-17). Upstream of the spec. Change this only by explicit agreement.

## Outcome
A personal pipeline that applies to jobs without the owner watching. It collects matching roles
into a queue, fills out applications when the laptop is open, submits the ones it is confident
about, and saves the rest as drafts for a quick review.

## User
One person: a full-time SWE on OPT who will not need sponsorship now or in the future (green
card via spouse), is moving to Kansas City in about 6 months, and wants as little screen time
after work as possible.

## Why
The market is rough. A steady flow of interviews keeps interview skills sharp (LeetCode prep in
case of a layoff) and opens a path to a better role. Volume of reasonable applications matters
more than perfect fit.

## Sources
- SimplifyJobs New-Grad README, Software Engineering section.
  *Note:* the table is HTML (`<table>`), not a markdown pipe table. There is an active table and
  a collapsed "Inactive roles" table, and closed roles show 🔒 instead of an apply link.
- Jobright feed: Liked jobs, plus jobs with a match above 80%. The feed is already filtered to
  H-1B-friendly companies.

## Fit filter
- **Titles:** new-grad and early-career SWE (SWE I/II, Backend, Full-stack, New Grad). Resume
  keywords must match the job description. 🎓 (advanced degree) roles are **included**.
- **Location priority:**
  1. Remote
  2. Kansas City metro, on-site or hybrid (e.g. Garmin, Olathe)
  3. Big, well-known companies in major hubs (SF, Seattle, NYC, Boston, DMV). "Big" means
     SimplifyJobs' 🔥 FAANG+ flag plus a company list the owner can edit.
- **Hard skip:**
  - senior and above
  - 🛂 (no sponsorship): these are usually government-adjacent employers with a strict I-9
    process
  - 🇺🇸 (citizenship required)
  - duplicates: never apply to the same company and role twice, even across sources

## Answering
- **Profile facts:** a single stored profile, answered the same way every time.
  Authorized to work: **yes**. Sponsorship now or in the future: **no**.
- **Preferences:** set once.
  - EEO questions, start date and similar are pre-set answers.
  - Salary is left blank unless the field is required. When it is required, look up real
    market data for that company and role (levels.fyi, Reddit, etc.) and never lowball.
    A minimum of $120k is the fallback.
- **Open-ended questions and cover letters:** an LLM writes them from the resume, tailored to the
  role and sounding human.

## Submit rule ("confident")
Submit only when **every required field** is filled according to the given instructions
(profile, preferences, or a valid LLM-written answer). If even one required field can't be
answered from those sources, **do not guess and do not fill it in as is**: save a draft and
notify the owner. A CAPTCHA also blocks auto-submit.

## Success
About 10 applications a day, 50+ a week. The owner's evening involvement is a short review
list (drafts plus "apply manually" links), not form-filling.

## V1 scope
Workday, Greenhouse and Lever, done well. Every other site is logged as "apply manually" with
its link.

## Out of scope (v1)
- Teaching the tool new portals by demonstration (e.g. Amazon): future feature
- Ashby, iCIMS, Oracle, SmartRecruiters and custom career sites
- On-site roles outside KC that are not at big-name companies
- Interview scheduling and follow-ups
- Any dashboard beyond the review list

## Open for the spec
- What a "draft" is on Greenhouse and Lever, since neither saves partial applications
- CAPTCHA handling on Greenhouse and Lever
- Where collection and application run: GitHub Actions, the laptop, or a home server
- LLM model choice and cost at 50+ applications a week
- How the salary lookup works
- Jobright access method and its terms of service
