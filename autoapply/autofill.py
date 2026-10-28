"""Fill in and submit applications on your behalf.

For each job: open the form, fill your contact details and resume, read every
remaining question, answer it from answers.yaml (or, for questions the rules
don't cover, with Claude using your resume), then click Submit and check that
the site confirmed it.

A job is skipped, never guessed at, when:
  - a required question has no confident answer        -> status needs_input
  - the site shows a CAPTCHA or an email security code  -> status captcha
  - the form could not be submitted                     -> status failed
  - Submit was clicked but no confirmation appeared     -> status unconfirmed (check by hand; not retried)
Skipped jobs can still be done by hand with `python3 -m autoapply apply`.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import date

import yaml

from .apply import form_url, prefill
from .db import Store

# Collects every unanswered question on the page. Each element gets a data-aa
# attribute so Python can find it again.
COLLECT_JS = r"""
() => {
  const vis = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return (r.width > 0 || r.height > 0) && s.visibility !== 'hidden' && s.display !== 'none'; };
  const clean = t => (t || '').replace(/\s+/g, ' ').trim();
  const box = el => el.closest('fieldset, [role=group], [role=radiogroup], .field, .application-question, [class*=question], [class*=field], li, .form-group') || el.parentElement;
  const labelOf = el => {
    if (el.labels && el.labels.length) return clean(el.labels[0].innerText);
    if (el.getAttribute('aria-label')) return clean(el.getAttribute('aria-label'));
    const lb = el.getAttribute('aria-labelledby');
    if (lb) { const t = lb.split(' ').map(i => document.getElementById(i)).filter(Boolean).map(e => e.innerText).join(' '); if (clean(t)) return clean(t); }
    const b = box(el);
    const l = b && b.querySelector('legend, label, [class*=label], [class*=title]');
    if (l && clean(l.innerText)) return clean(l.innerText);
    return clean(el.getAttribute('placeholder') || el.name || '');
  };
  const groupLabel = el => {
    const b = el.closest('fieldset, [role=radiogroup], [role=group], .application-question, [class*=question], .field, li') || box(el);
    const l = b && b.querySelector('legend, [class*=label]:not(input), [class*=title], label');
    return clean(l ? l.innerText : '');
  };
  const req = (el, label) => el.required || el.getAttribute('aria-required') === 'true' || /\*\s*$|\*\s*\(|\(required\)/i.test(label);
  let n = 0; const out = []; const groups = {};
  for (const el of document.querySelectorAll('input, textarea, select, [role=combobox]')) {
    const tag = el.tagName.toLowerCase(); const type = (el.getAttribute('type') || tag).toLowerCase();
    if (['hidden', 'submit', 'button', 'file', 'image', 'reset', 'search'].includes(type)) continue;
    if (el.closest('[aria-hidden=true]') || el.disabled) continue;
    if (type === 'radio' || type === 'checkbox') {
      const key = el.name || ('g' + (box(el) ? Array.from(document.querySelectorAll('*')).indexOf(box(el)) : n));
      if (!groups[key]) { groups[key] = { kind: type, label: groupLabel(el), options: [], required: false, answered: false }; out.push(groups[key]); }
      const g = groups[key]; const id = 'aa' + (n++); el.setAttribute('data-aa', id);
      const optLabel = clean((el.labels && el.labels[0] && el.labels[0].innerText) || el.getAttribute('aria-label') || el.value);
      g.options.push({ id, text: optLabel }); g.required = g.required || req(el, g.label);
      if (!g.label) g.label = optLabel;  // a lone checkbox labelled by its own text
      if (el.checked) g.answered = true;
      continue;
    }
    if (!vis(el) && el.getAttribute('role') !== 'combobox') continue;
    const id = 'aa' + (n++); el.setAttribute('data-aa', id);
    const label = labelOf(el);
    const f = { id, kind: el.getAttribute('role') === 'combobox' && tag === 'input' ? 'combobox' : (tag === 'select' ? 'select' : (tag === 'textarea' ? 'textarea' : 'text')),
                label, required: req(el, label), answered: false, options: [] };
    if (tag === 'select') { f.options = Array.from(el.options).filter(o => o.value).map(o => ({ id, text: clean(o.text) })); f.answered = !!el.value; }
    else if (f.kind === 'combobox') { const sv = box(el) && box(el).querySelector('[class*=single-value], [class*=singleValue]'); f.answered = !!(sv && clean(sv.innerText)) || !!el.value; }
    else f.answered = !!el.value;
    out.push(f);
  }
  // Ashby-style Yes/No buttons with no form input behind them.
  for (const c of document.querySelectorAll('fieldset, [class*=fieldEntry], [class*=question]')) {
    if (c.querySelector('input:not([type=hidden]), textarea, select')) continue;
    const btns = Array.from(c.querySelectorAll('button')).filter(vis);
    if (btns.length < 2 || btns.length > 6) continue;
    const label = clean((c.querySelector('legend, label, [class*=label], [class*=title]') || {}).innerText || '');
    if (!label) continue;
    const g = { kind: 'buttons', label, options: [], required: /\*/.test(label), answered: btns.some(b => b.getAttribute('aria-pressed') === 'true' || /active|selected/i.test(b.className)) };
    for (const b of btns) { const id = 'aa' + (n++); b.setAttribute('data-aa', id); g.options.push({ id, text: clean(b.innerText) }); }
    out.push(g);
  }
  return out.filter(f => !f.answered && f.label);
}
"""

CONSENT = re.compile(r"\b(i agree|i acknowledge|i consent|i certify|i have read|privacy (policy|notice)|terms)\b", re.I)
CONFIRM = re.compile(
    r"thank(s| you) for (applying|your (application|interest))|application (has been |was )?(submitted|received)"
    r"|we('ve| have) received your application|successfully (submitted|applied)", re.I)
OPT_IN = re.compile(r"\b(sms|text messages?|marketing|newsletter|promotional|future (roles|opportunities))\b", re.I)
VERIFY = re.compile(r"(security|verification) code|verify (it'?s|that it'?s) you|check your email for a code", re.I)


def load_answers(path: str) -> dict:
    if not os.path.exists(path):
        raise SystemExit(f"{path} not found. Run: cp answers.example.yaml answers.yaml, then fill it in.")
    with open(path) as f:
        a = yaml.safe_load(f)
    text = open(path).read()
    if "CHANGE_ME" in text:
        raise SystemExit(f"{path} still has CHANGE_ME answers. Fill them in before auto-submitting.")
    if not a.get("confirmed"):
        raise SystemExit(f"Set `confirmed: true` in {path} once every answer is correct for you.")
    return a


def rule_answer(label: str, answers: dict) -> str | None:
    for r in answers.get("rules", []):
        if any(re.search(p, label, re.I) for p in r["match"]):
            return str(r["answer"])
    return None


def pick_option(answer: str, options: list[dict]) -> dict | None:
    """Find the option that best matches a text answer."""
    a = answer.strip().lower()
    texts = [(o, o["text"].strip().lower()) for o in options]
    for test in (lambda t: t == a, lambda t: t.startswith(a), lambda t: a.startswith(t) and len(t) > 1,
                 lambda t: a in t, lambda t: t in a and len(t) > 2):
        hit = [o for o, t in texts if t and test(t)]
        if hit:
            return hit[0]
    return None


def resume_text(path: str) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        raise SystemExit("pypdf is not installed. Run: pip install -r requirements.txt")
    logging.getLogger("pypdf").setLevel(logging.ERROR)  # harmless warnings on many exported PDFs
    return "\n".join(p.extract_text() or "" for p in PdfReader(path).pages)


def ask_claude(questions: list[dict], job: dict, resume: str, answers: dict) -> dict:
    """One call per application. Returns {question_id: answer} for the ones Claude is confident about."""
    import anthropic

    qs = [{"id": q["id"], "question": q["label"], "type": q["kind"], "required": q["required"],
           "options": [o["text"] for o in q["options"]] or None} for q in questions]
    system = (
        "You fill in job application forms for one candidate, using only facts from the candidate's resume "
        "and profile below. Answer truthfully. Never invent experience, degrees, employers, numbers or "
        "legal status the materials don't support. For a question with options, answer with the exact "
        "text of one option (for checkboxes, a list of option texts joined by ' | '). For free-text "
        "questions such as 'Why do you want to work here?', write 2-4 specific sentences in first person "
        "that connect the resume to the job. Set confident to false whenever the materials don't settle "
        "the answer, rather than guessing.\n\n"
        f"CANDIDATE PROFILE:\n{yaml.safe_dump(answers.get('facts', {}), sort_keys=True)}\n\n"
        f"RESUME:\n{resume}"
    )
    user = (f"JOB: {job['title']} at {job['company']} ({job['location']})\n\n"
            f"JOB DESCRIPTION (excerpt):\n{(job.get('description') or '')[:6000]}\n\n"
            f"QUESTIONS:\n{json.dumps(qs, indent=1)}")
    schema = {
        "type": "object",
        "properties": {"answers": {"type": "array", "items": {
            "type": "object",
            "properties": {"id": {"type": "string"}, "answer": {"type": "string"}, "confident": {"type": "boolean"}},
            "required": ["id", "answer", "confident"], "additionalProperties": False}}},
        "required": ["answers"], "additionalProperties": False,
    }
    client = anthropic.Anthropic()
    resp = client.beta.messages.create(
        model=answers.get("claude_model", "claude-opus-5-5"),
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": schema}},
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
    )
    if resp.stop_reason != "end_turn":
        return {}
    text = next((b.text for b in resp.content if b.type == "text"), "{}")
    return {a["id"]: a["answer"] for a in json.loads(text)["answers"] if a["confident"] and a["answer"].strip()}


def set_field(page, q: dict, answer: str) -> bool:
    loc = lambda i: page.locator(f"[data-aa='{i}']").first
    try:
        if q["kind"] in ("text", "textarea"):
            loc(q["id"]).fill(answer)
        elif q["kind"] == "select":
            opt = pick_option(answer, q["options"])
            if not opt:
                return False
            loc(q["id"]).select_option(label=opt["text"])
        elif q["kind"] in ("radio", "buttons"):
            opt = pick_option(answer, q["options"])
            if not opt:
                return False
            loc(opt["id"]).click(force=True)
        elif q["kind"] == "checkbox":
            picks = [pick_option(a, q["options"]) for a in answer.split("|")]
            picks = [p for p in picks if p]
            if not picks:
                return False
            for p in picks:
                loc(p["id"]).check(force=True)
        elif q["kind"] == "combobox":
            box = loc(q["id"])
            box.click()
            box.fill(answer[:40])
            page.wait_for_timeout(700)
            options = page.locator("[role=option]")
            texts = [options.nth(i).inner_text() for i in range(min(options.count(), 50))]
            opt = pick_option(answer, [{"id": str(i), "text": t} for i, t in enumerate(texts)])
            if not opt:
                box.fill("")
                page.keyboard.press("Escape")
                return False
            options.nth(int(opt["id"])).click()
        return True
    except Exception:
        return False


def captcha_visible(page) -> bool:
    for sel in ("iframe[src*='recaptcha/api2/bframe']", "iframe[src*='hcaptcha.com']", "iframe[title*='challenge' i]"):
        frames = page.locator(sel)
        for i in range(frames.count()):
            try:
                if frames.nth(i).is_visible():
                    box = frames.nth(i).bounding_box()
                    if box and box["height"] > 100:
                        return True
            except Exception:
                pass
    return False


def click_submit(page) -> bool:
    for sel in ("button[type=submit]", "input[type=submit]", "#submit_app",
                "button:has-text('Submit application')", "button:has-text('Submit Application')",
                "button:has-text('Submit')", "button:has-text('Apply')"):
        b = page.locator(sel)
        for i in range(b.count()):
            if b.nth(i).is_visible() and b.nth(i).is_enabled():
                b.nth(i).click()
                return True
    return False


def wait_for_result(page, timeout_ms=20000) -> str:
    """applied | captcha | unconfirmed (clicked Submit, but the site never said thanks)"""
    waited = 0
    while waited < timeout_ms:
        page.wait_for_timeout(1000)
        waited += 1000
        try:
            body = page.inner_text("body")
        except Exception:
            continue
        if CONFIRM.search(body) or re.search(r"confirmation|thank", page.url, re.I):
            return "applied"
        if VERIFY.search(body) or captcha_visible(page):
            return "captcha"
    return "unconfirmed"


def apply_one(page, row, job: dict, cfg: dict, answers: dict, resume_pdf: str, resume_txt: str,
              use_llm: bool, dry_run: bool) -> tuple[str, dict]:
    page.goto(form_url(row), wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(2500)
    # Lever and some Ashby pages show the description first with an Apply button.
    for sel in ("a:has-text('Apply for this job')", "button:has-text('Apply for this Job')", "a:has-text('Apply now')"):
        if page.locator(sel).count() and page.locator(sel).first.is_visible():
            page.locator(sel).first.click()
            page.wait_for_timeout(2000)
            break
    if captcha_visible(page):
        return "captcha", {}
    filled = prefill(page, row["source"], cfg["profile"], resume_pdf)
    log = {"prefilled": filled, "answers": {}, "unanswered": []}

    questions = page.evaluate(COLLECT_JS)
    leftovers = []
    for q in questions:
        text = q["label"] + " " + (q["options"][0]["text"] if q["options"] else "")
        if q["kind"] == "checkbox" and len(q["options"]) == 1 and CONSENT.search(text) and not OPT_IN.search(text):
            if set_field(page, q, q["options"][0]["text"]):
                log["answers"][q["label"]] = "checked"
            continue
        ans = rule_answer(q["label"], answers)
        if ans is not None and set_field(page, q, ans):
            log["answers"][q["label"]] = ans
        else:
            leftovers.append(q)

    if leftovers and use_llm:
        try:
            ai = ask_claude(leftovers, job, resume_txt, answers)
        except Exception as e:
            print(f"    Claude call failed: {type(e).__name__}: {e}")
            ai = {}
        still = []
        for q in leftovers:
            if q["id"] in ai and set_field(page, q, ai[q["id"]]):
                log["answers"][q["label"]] = ai[q["id"]]
            else:
                still.append(q)
        leftovers = still

    missing = [q["label"] for q in leftovers if q["required"]]
    log["unanswered"] = [q["label"] for q in leftovers]
    if missing:
        log["missing_required"] = missing
        return "needs_input", log
    if dry_run:
        return "dry_run", log
    if not click_submit(page):
        log["error"] = "no submit button found"
        return "failed", log
    return wait_for_result(page), log


def run(store: Store, cfg: dict, limit: int, dry_run: bool, headless: bool, here: str):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SystemExit("Playwright is not installed. Run: pip install -r requirements.txt && playwright install chromium")

    answers = load_answers(os.path.join(here, cfg.get("answers_file", "answers.yaml")))
    resume_pdf = os.path.abspath(os.path.expanduser(cfg["profile"]["resume_path"]))
    if not os.path.exists(resume_pdf):
        raise SystemExit(f"Resume not found at {resume_pdf}. Set profile.resume_path in config.yaml.")
    resume_txt = resume_text(resume_pdf)
    use_llm = bool(os.environ.get("ANTHROPIC_API_KEY")) and answers.get("use_claude", True)
    if not use_llm:
        print("No ANTHROPIC_API_KEY set: only questions covered by answers.yaml will be answered.\n")

    left = max(limit - store.applied_today(), 0)
    rows = store.by_status("approved", "queued")[:left]
    if not rows:
        print(f"Nothing to apply to (applied today: {store.applied_today()}, cap {limit}). Run `daily` first.")
        return
    shots = os.path.join(here, "logs", date.today().isoformat())
    os.makedirs(shots, exist_ok=True)
    totals = {}
    print(f"{'Dry run: filling but not submitting' if dry_run else 'Applying'} to {len(rows)} jobs...\n")

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            os.path.expanduser(cfg.get("browser_profile_dir", "~/.autoapply-browser")), headless=headless)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        for i, r in enumerate(rows, 1):
            print(f"[{i}/{len(rows)}] {r['company']} | {r['title']}")
            job = dict(r)
            try:
                status, log = apply_one(page, r, job, cfg, answers, resume_pdf, resume_txt, use_llm, dry_run)
            except Exception as e:
                status, log = "failed", {"error": f"{type(e).__name__}: {e}"}
            name = re.sub(r"[^A-Za-z0-9]+", "_", f"{r['company']}_{r['title']}")[:80]
            try:
                page.screenshot(path=os.path.join(shots, f"{name}.png"), full_page=True)
            except Exception:
                pass
            totals[status] = totals.get(status, 0) + 1
            msg = {"applied": "submitted", "dry_run": "filled (not submitted)", "needs_input": "skipped, needs your answer",
                   "captcha": "skipped, CAPTCHA or email code", "failed": "skipped, could not submit",
                   "unconfirmed": "submitted, but the site did not confirm (check the screenshot)"}[status]
            print(f"    {msg}")
            for q in log.get("missing_required", []):
                print(f"      no answer for: {q}")
            if log.get("error"):
                print(f"      {log['error']}")
            if status != "dry_run":
                store.set_status(r["key"], status, json.dumps(log))
        ctx.close()
    print(f"\nDone: {totals}. Screenshots in {shots}")
    if totals.get("needs_input"):
        print("Add rules to answers.yaml for the questions listed above, then run `retry`.")
