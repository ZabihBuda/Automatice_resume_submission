# Automatic Resume Submission

A personal job-search assistant for data science and machine learning roles at US
companies. Every day it finds up to 30 new openings that fit your profile, ranks them
against the skills on your resume, and applies to them for you: it fills in your details,
uploads your resume, answers the form's questions from your answers file (and, for
free-text questions, with Claude using your resume), clicks Submit, and checks that the
company confirmed it.

**What it does**

- Scans the public job boards of about 50 US tech companies (Greenhouse, Lever, Ashby)
- Keeps data scientist, ML engineer, applied scientist, NLP and LLM roles in the US or remote
- Drops roles that need US citizenship or a security clearance, and flags "no sponsorship"
  postings (useful for TN visa holders)
- Ranks matches by resume keyword overlap, with a boost for preferred cities
- Never queues the same job twice, and caps applications per company
- Keeps a full log of every application in a local SQLite database
- Skips, rather than guesses, any form with a question it can't answer, a CAPTCHA, or an
  email security code, and tells you which ones need you

**Built with** Python, SQLite, Playwright, the Claude API, and the public Greenhouse,
Lever and Ashby job board APIs.

## How it works

```
run (once a day)
fetch ~50 job boards → filter + score + dedupe → top 30
  → for each job: fill details + resume → answer questions → Submit → check for "thank you"
  → applied / needs_input / captcha / unconfirmed, saved to applications.db + a screenshot
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
5. **Answering questions.** Every question on the form is matched against the rules in
   `answers.yaml` (work authorization, sponsorship, relocation, start date, EEO, and so on).
   Anything left over goes to Claude in one request per application, with your resume, the
   job description and the `facts` in `answers.yaml`. Claude is told to answer only from
   those and to mark a question "not confident" rather than guess.
6. **Submitting.** If every required question has an answer, the tool clicks Submit and
   waits for the confirmation page. It skips the job, without submitting, when a required
   question is still unanswered (`needs_input`), or a CAPTCHA or email code appears
   (`captcha`). A screenshot of every form is saved in `logs/<date>/`.
7. **Log.** `python3 -m autoapply log` lists every application with its date and status.

Prefer to check each one yourself? `daily`, `review` and `apply` still work as before:
the browser opens each approved form pre-filled and you click Submit.

## Setup (once, Mac)

```bash
cd ~/Documents/Automatice_resume_submission
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp config.example.yaml config.yaml      # your contact details and resume_path
cp answers.example.yaml answers.yaml    # your answers to common questions
open -e answers.yaml                    # replace each CHANGE_ME, then set confirmed: true
```

For free-text questions ("Why do you want to work here?"), get an API key at
console.anthropic.com and add it to your shell once:

```bash
echo 'export ANTHROPIC_API_KEY=sk-ant-...' >> ~/.zshrc && source ~/.zshrc
```

Without a key the tool still runs, but skips every form that has a required free-text question.

## Every day

```bash
cd ~/Documents/Automatice_resume_submission && source .venv/bin/activate
python3 -m autoapply run            # find today's jobs and apply to up to 30
python3 -m autoapply log            # what was submitted
```

Try `python3 -m autoapply run --dry-run` first: it fills the forms but doesn't submit,
so you can check the screenshots in `logs/` before anything goes to an employer.

When jobs are skipped with `needs_input`, the output lists the questions. Add a rule for
each one to `answers.yaml`, then run `python3 -m autoapply retry` and `python3 -m autoapply auto`.

### Run it automatically every morning

`crontab -e`, then add (keep the Mac awake and logged in at that time):

```
0 8 * * * cd ~/Documents/Automatice_resume_submission && .venv/bin/python -m autoapply run >> run.log 2>&1
```

Cron doesn't read `~/.zshrc`, so put `ANTHROPIC_API_KEY=sk-ant-... ` at the start of that command.

## Risks and why the tool is built this way

- **LinkedIn and Indeed are not used.** Both ban automated applying in their terms and
  actively detect it. Accounts get restricted or permanently banned, and a banned LinkedIn
  account hurts a job search far more than 30 applications help it.
- **CAPTCHAs are never bypassed.** Some company forms show one, and some Greenhouse forms
  email you a security code. Those jobs are skipped and listed so you can do them by hand.
- **Answers go out as written.** The work-authorization and sponsorship answers in
  `answers.yaml` are yours to decide, and the tool won't run until you've filled them in
  and set `confirmed: true`. Claude's free-text answers are saved in `applications.db` and
  shown in the screenshots, so you can check what was sent.
- **Claude API cost.** One request per application that has questions the rules don't
  cover: roughly a few cents each, so about $1 to $2 a day at 30 applications.
- **Quality vs. volume.** 30 a day is about 600 a month. Recruiters at the same company
  can see every application you send, so the 3-per-company cap stays on by default.

## Tests

```bash
pip install pytest && python -m pytest tests
```
