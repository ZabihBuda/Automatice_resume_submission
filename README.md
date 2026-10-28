# Automatic Resume Submission

A personal job-search assistant for data science and machine learning roles at US
companies. Every day it finds up to 30 new openings that fit your profile, ranks them
against the skills on your resume, and lines them up for a quick review. For each job you
approve, it opens the application form in a browser with your details and resume already
filled in, so applying takes about a minute instead of ten.

**What it does**

- Scans the public job boards of about 50 US tech companies (Greenhouse, Lever, Ashby)
- Keeps data scientist, ML engineer, applied scientist, NLP and LLM roles in the US or remote
- Drops roles that need US citizenship or a security clearance, and flags "no sponsorship"
  postings (useful for TN visa holders)
- Ranks matches by resume keyword overlap, with a boost for preferred cities
- Never queues the same job twice, and caps applications per company
- Keeps a full log of every application in a local SQLite database
- Leaves the final Submit click to you, so no account or application gets flagged

**Built with** Python, SQLite, Playwright, and the public Greenhouse, Lever and Ashby
job board APIs.

## How it works

```
daily (scheduled)        review (you, ~5 min)     apply (you, ~1 min per job)
fetch ~50 job boards  →  y / n per job        →   browser opens each form, pre-filled
filter + score + dedupe                           you answer custom questions, click Submit
write queue/<date>.html                           status saved to applications.db
```

1. **Job sources.** Public Greenhouse, Lever and Ashby job board APIs (see `companies.yaml`).
   These are open, read-only feeds that companies publish so their jobs can be listed
   elsewhere. No login, no scraping, no ban risk. Most US tech companies use one of the three.
2. **Filtering.** Title must match a data science / ML keyword and must not be intern,
   director, principal, manager and so on. Location must be in the US or US-remote. Postings
   that require US citizenship or a security clearance are dropped. Postings that say "we
   don't sponsor" are kept but flagged, because TN status does not need H-1B sponsorship
   and many of those employers will still hire on TN.
3. **Ranking.** Score = how many of your resume skills the posting mentions, plus a bonus
   for senior / NLP / LLM titles, and +15 for Syracuse or Virginia (`preferred_locations`
   in config). Any other US city or remote still counts. At most 3 jobs per company per day.
4. **Dedupe.** Every job ever seen is stored in `applications.db` (SQLite). A job is never
   queued twice, and a repost with the same title at the same company is skipped.
5. **Review step.** Nothing reaches an employer until you approve it with `review`.
6. **Applying.** Playwright opens a Chrome window with up to 5 application tabs at a time
   (`tabs_at_once` in config), fills name, email, phone, LinkedIn,
   GitHub and uploads your resume, then waits. Custom questions (work authorization,
   salary, "why us") are left for you, because wrong automated answers there get
   applications rejected. You click Submit, then press `a` in the terminal to log it.
7. **Log.** `python -m autoapply log` lists every application with its date.

## Setup (once)

```bash
cd job-autoapply
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
cp config.example.yaml config.yaml                     # then check resume_path and preferences
```

## Every day

```bash
python -m autoapply daily --open   # fetch + build today's queue, opens the report
python -m autoapply review         # y / n each job
python -m autoapply apply          # fill and submit approved ones (stops at 30 per day)
python -m autoapply log            # history
```

### Run `daily` automatically at 8am

- **Mac/Linux:** `crontab -e`, then add
  `0 8 * * * cd /path/to/job-autoapply && .venv/bin/python -m autoapply daily >> daily.log 2>&1`
- **Windows:** Task Scheduler → Create Basic Task → Daily 8:00 → Program
  `C:\path\to\job-autoapply\.venv\Scripts\python.exe`, arguments `-m autoapply daily`,
  start in `C:\path\to\job-autoapply`.

## Risks and why the tool is built this way

- **LinkedIn and Indeed are not used.** Both ban automated applying in their terms and
  actively detect it. Accounts get restricted or permanently banned, and a banned LinkedIn
  account hurts a job search far more than 30 applications help it.
- **Fully unattended submission is not built in.** Many forms have CAPTCHAs, and
  questions like "Will you require sponsorship?" need a correct, human answer. A wrong
  auto-answer can quietly disqualify you. The review + click-Submit step keeps that safe.
- **Quality vs. volume.** 30 a day is about 600 a month. Recruiters at the same company
  can see every application you send, so the 3-per-company cap stays on by default.

## Tests

```bash
pip install pytest && python -m pytest tests
```
