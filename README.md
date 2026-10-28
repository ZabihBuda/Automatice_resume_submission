# Daily data science job auto-apply

Finds up to 30 new US data science / ML roles a day, ranks them against your resume,
lets you approve them in a quick review, then opens each application in a browser with
your details and resume already filled in. You click Submit.

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
6. **Applying.** Playwright opens a real Chrome window, fills name, email, phone, LinkedIn,
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
