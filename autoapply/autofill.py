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

import html
import json
import logging
import os
import re
from datetime import date

import yaml

from .apply import form_url, prefill
from .db import Store
from .filters import title_matches

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
  // The heading above a field ("Employment", "Education"), to tell apart fields with the same label.
  const heads = Array.from(document.querySelectorAll('h1, h2, h3, h4'));
  const sectionOf = el => { let s = ''; for (const h of heads) {
    if (h.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING) s = clean(h.innerText); else break; } return s; };
  const req = (el, label) => el.required || el.getAttribute('aria-required') === 'true' || /\*\s*$|\*\s*\(|\(required\)/i.test(label);
  let n = 0, gid = 0; const out = []; const groups = {}; const boxIds = new Map();
  for (const el of document.querySelectorAll('input, textarea, select, [role=combobox]')) {
    const tag = el.tagName.toLowerCase(); const type = (el.getAttribute('type') || tag).toLowerCase();
    if (['hidden', 'submit', 'button', 'file', 'image', 'reset', 'search'].includes(type)) continue;
    if (el.closest('[aria-hidden=true]') || el.disabled) continue;
    if (type === 'radio' || type === 'checkbox') {
      const b = box(el);
      if (b && !boxIds.has(b)) boxIds.set(b, 'g' + (gid++));
      const key = el.name || (b ? boxIds.get(b) : 'n' + n);
      if (!groups[key]) { groups[key] = { id: 'grp' + out.length, kind: type, label: groupLabel(el), section: sectionOf(el), options: [], required: false, answered: false }; out.push(groups[key]); }
      const g = groups[key]; const id = 'aa' + (n++); el.setAttribute('data-aa', id);
      const optLabel = clean((el.labels && el.labels[0] && el.labels[0].innerText) || el.getAttribute('aria-label') || el.value);
      g.options.push({ id, text: optLabel }); g.required = g.required || req(el, g.label);
      if (!g.label) g.label = optLabel;  // a lone checkbox labelled by its own text
      if (el.checked) g.answered = true;
      continue;
    }
    if (!vis(el) && el.getAttribute('role') !== 'combobox') continue;
    if (tag !== 'input' && el.querySelector('input, select, textarea')) continue;  // the input inside is collected instead
    const id = 'aa' + (n++); el.setAttribute('data-aa', id);
    const label = labelOf(el);
    const f = { id, kind: el.getAttribute('role') === 'combobox' ? 'combobox' : (tag === 'select' ? 'select' : (tag === 'textarea' ? 'textarea' : 'text')),
                label, section: sectionOf(el), required: req(el, label), answered: false, options: [] };
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
    const g = { id: 'grp' + out.length, kind: 'buttons', label, section: sectionOf(c), options: [], required: /\*/.test(label), answered: btns.some(b => b.getAttribute('aria-pressed') === 'true' || /active|selected/i.test(b.className)) };
    for (const b of btns) { const id = 'aa' + (n++); b.setAttribute('data-aa', id); g.options.push({ id, text: clean(b.innerText) }); }
    out.push(g);
  }
  return out.filter(f => !f.answered && f.label);
}
"""

# Shown at the top of every page the tool opens, so nobody starts filling a form by hand.
BANNER_JS = r"""
(() => {
  const add = () => {
    if (document.getElementById('autoapply-banner') || !document.body) return;
    const b = document.createElement('div');
    b.id = 'autoapply-banner';
    b.textContent = 'MESSAGE';
    b.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:2147483647;pointer-events:none;'
      + 'background:#1d4ed8;color:#fff;font:600 15px system-ui,sans-serif;padding:10px 16px;text-align:center;'
      + 'box-shadow:0 2px 8px rgba(0,0,0,.25)';
    document.body.appendChild(b);
  };
  document.readyState === 'loading' ? document.addEventListener('DOMContentLoaded', add) : add();
})();
"""

CONSENT = re.compile(r"\b(i agree|i acknowledge|i consent|i certify|i have read|i understand|privacy (policy|notice|acknowledge?ment)"
                     r"|review and acknowledge|acknowledge?ment|confirm receipt|attestation|i attest|ai (usage|policy)|personal data"
                     r"|terms (and conditions|of (use|service)))\b", re.I)
CONFIRM = re.compile(
    r"thank(s| you) for (applying|your (application|interest))|application (has been |was )?(submitted|received)"
    r"|we('ve| have) received your application|successfully (submitted|applied)", re.I)
OPT_IN = re.compile(r"\b(sms|text messages?|marketing|newsletter|promotional|future (roles|opportunities))\b", re.I)
VERIFY = re.compile(r"(security|verification) code|verify (it'?s|that it'?s) you|check your email for a code"
                    r"|code (was |has been )?sent to your email|enter the \d+-character code", re.I)

# Dropdown options on the page, in the order a person would see them. Tags each with data-aa-opt.
OPTIONS_JS = r"""
() => {
  const vis = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  document.querySelectorAll('[data-aa-opt]').forEach(el => el.removeAttribute('data-aa-opt'));
  for (const sel of ["[role=listbox] [role=option]", "[role=option]", "[id*='-option-']", ".select__option", "[class*=option]"]) {
    const els = Array.from(document.querySelectorAll(sel)).filter(vis)
      .filter(el => !el.querySelector(sel) && (el.innerText || '').trim());
    if (els.length) return els.map((el, i) => { el.setAttribute('data-aa-opt', i); return el.innerText.replace(/\s+/g, ' ').trim(); });
  }
  return [];
}
"""

# Error messages a form shows after a rejected Submit ("This field is required").
ERRORS_JS = r"""
() => {
  const vis = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const clean = t => (t || '').replace(/\s+/g, ' ').trim();
  const out = [];
  for (const el of document.querySelectorAll('[class*=error], [class*=Error], [role=alert], [aria-live]')) {
    const t = clean(el.innerText);
    if (!vis(el) || !t || t.length > 200 || !/required|invalid|please|must|blank|error/i.test(t)) continue;
    const box = el.closest('.field, .select, fieldset, [class*=question], [class*=field], li');
    const l = box && box.querySelector('label, legend');
    out.push((l && clean(l.innerText) && clean(l.innerText) !== t ? clean(l.innerText) + ': ' : '') + t);
  }
  return Array.from(new Set(out)).slice(0, 5);
}
"""


EXAMPLE_ANSWERS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "answers.example.yaml")


def _as_text(v) -> str:
    # YAML reads a bare yes/no as true/false; forms want "Yes"/"No".
    return {True: "Yes", False: "No"}.get(v, v) if isinstance(v, bool) else ("" if v is None else str(v))


def _answers_of(r: dict) -> list[str]:
    v = r.get("answer", "")
    return [_as_text(x) for x in (v if isinstance(v, list) else [v])]


def _unfilled(r: dict) -> bool:
    return any(x.strip().upper() == "CHANGE_ME" for x in r["answer"])


def norm_question(label: str) -> str:
    return re.sub(r"[\s*:?]+$", "", re.sub(r"\s+", " ", label)).strip().lower()


def load_answers(path: str) -> dict:
    if not os.path.exists(path):
        raise SystemExit(f"{path} not found. Run: cp answers.example.yaml answers.yaml, then fill it in.")
    with open(path) as f:
        a = yaml.safe_load(f)
    rules = a.get("rules") or []
    for r in rules:
        r["answer"] = _answers_of(r)
    # Only real answers count; the word also appears in the file's comments.
    unfilled = [r["match"][0] for r in rules if _unfilled(r)]
    if unfilled:
        raise SystemExit(f"{path} still has CHANGE_ME answers for: {', '.join(unfilled)}. "
                         "Fill them in before auto-submitting.")
    if not a.get("confirmed"):
        raise SystemExit(f"Set `confirmed: true` in {path} once every answer is correct for you.")
    a["rules"] = saved_answers(os.path.join(os.path.dirname(os.path.abspath(path)), UNANSWERED)) + rules + default_rules(rules)
    return a


UNANSWERED = "unanswered.yaml"
UNANSWERED_HEADER = """\
# Questions the tool couldn't answer on its own. Type your answer after `answer:` (in quotes),
# save, and every later application that asks the same question uses it.
# For a question with `choices`, copy the choice you want. Leave an answer empty to keep
# skipping jobs that ask it. Then run:  python3 -m autoapply retry && python3 -m autoapply auto
"""


def saved_answers(path: str) -> list[dict]:
    """Your answers from unanswered.yaml, as rules that match that exact question."""
    if not os.path.exists(path):
        return []
    with open(path) as f:
        items = yaml.safe_load(f) or []
    out = []
    for it in items:
        ans = [x for x in _answers_of(it) if x.strip()]
        if it.get("question") and ans:
            out.append({"exact": norm_question(it["question"]), "match": [], "answer": ans})
    return out


def record_unanswered(path: str, questions: list[dict], company: str) -> int:
    """Add questions not already in unanswered.yaml. Returns how many were new."""
    items = []
    if os.path.exists(path):
        with open(path) as f:
            items = yaml.safe_load(f) or []
    known = {norm_question(it.get("question", "")) for it in items}
    new = []
    for q in questions:
        key = norm_question(q["label"])
        if key in known:
            continue
        known.add(key)
        it = {"question": re.sub(r"[\s*]+$", "", q["label"]).strip(), "asked_by": company}
        choices = [o["text"] for o in q.get("options", []) if o.get("text")][:40]
        if choices:
            it["choices"] = choices
        if q.get("tried"):
            it["note"] = "tried " + " / ".join(q["tried"][:3]) + ", but that isn't one of the choices"
        it["answer"] = ""
        new.append(it)
    if new:
        with open(path, "w") as f:
            f.write(UNANSWERED_HEADER + "\n")
            yaml.safe_dump(items + new, f, sort_keys=False, allow_unicode=True, width=100)
    return len(new)


def default_rules(own: list[dict]) -> list[dict]:
    """Rules from answers.example.yaml, used after your own so new ones arrive with `git pull`.
    A CHANGE_ME rule takes your answer from your rule with the same first pattern, or is dropped."""
    if not os.path.exists(EXAMPLE_ANSWERS):
        return []
    with open(EXAMPLE_ANSWERS) as f:
        example = yaml.safe_load(f).get("rules") or []
    mine = {r["match"][0]: r["answer"] for r in own if r.get("match")}
    out = []
    for r in example:
        r["answer"] = _answers_of(r)
        if _unfilled(r):
            if r["match"][0] not in mine:
                continue
            r["answer"] = mine[r["match"][0]]
        out.append(r)
    return out


def rule_answers(label: str, answers: dict, job: dict | None = None, use_llm: bool = False,
                 section: str = "") -> list[str]:
    """Answers from every rule that matches, best first: rules for this part of the form
    (`section:`, e.g. Employment), then your saved and own rules, then the defaults. A text box
    uses the first; a dropdown the first that is one of its choices. {name} placeholders are filled
    from `job` (the job plus your profile); an answer whose placeholder has no value is left out."""
    values = {k: str(v).strip() for k, v in (job or {}).items() if v is not None}
    key = norm_question(label)
    rules = answers.get("rules", [])
    ordered = [r for r in rules if r.get("section")] + [r for r in rules if not r.get("section")]
    out, section_hit = [], False
    for r in ordered:
        if use_llm and r.get("only_without_claude"):
            continue
        if r.get("section") and not re.search(r["section"], section or "", re.I):
            continue
        if not (r.get("exact") == key or any(re.search(p, label, re.I) for p in r.get("match", []))):
            continue
        if section_hit and not r.get("section"):
            break  # a section rule decided it; general rules ("start date") mean something else here
        section_hit = section_hit or bool(r.get("section"))
        for text in (r["answer"] if isinstance(r["answer"], list) else _answers_of(r)):
            keys = re.findall(r"\{(\w+)\}", text)
            if any(not values.get(k) for k in keys):
                continue
            for k in keys:
                text = text.replace("{" + k + "}", values[k])
            if text.strip() and text not in out:
                out.append(text)
    return out


def rule_answer(label: str, answers: dict, job: dict | None = None, use_llm: bool = False,
                section: str = "") -> str | None:
    """The best answer from the matching rules."""
    out = rule_answers(label, answers, job, use_llm, section)
    return out[0] if out else None


DECLINE = re.compile(r"decline|prefer not|not to (say|answer|disclose|self)|(don'?t|do not) (wish|want) to (answer|say|disclose)"
                     r"|choose not|rather not|not (wish|want) to (answer|disclose)", re.I)
AGREE_ANSWER = "(agree)"  # picks whichever option says yes / I agree / I acknowledge
AGREE = re.compile(r"^(yes|i agree|agree|i acknowledge|acknowledged?|i accept|accept|i consent|i understand|understood"
                   r"|i confirm|confirmed|i attest|i certify|i have read|i will comply)\b", re.I)


def pick_option(answer: str, options: list[dict]) -> dict | None:
    """Find the option that best matches a text answer."""
    a = answer.strip().lower()
    texts = [(o, o["text"].strip().lower()) for o in options]
    if a == AGREE_ANSWER:
        return next((o for o, t in texts if AGREE.search(t)), None)
    starts = lambda t, x: re.match(re.escape(x) + r"(\b|$)", t) is not None  # "no" fits "no, i don't", not "not a veteran"
    for test in (lambda t: t == a, lambda t: starts(t, a), lambda t: starts(a, t) and len(t) > 1,
                 lambda t: len(a) > 3 and a in t, lambda t: t in a and len(t) > 2):
        hit = [o for o, t in texts if t and test(t)]
        if hit:
            return hit[0]
    if DECLINE.search(a):  # "Decline to self-identify" vs "I don't wish to answer"
        return next((o for o, t in texts if DECLINE.search(t)), None)
    return None


def resume_text(path: str) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        raise SystemExit("pypdf is not installed. Run: pip install -r requirements.txt")
    logging.getLogger("pypdf").setLevel(logging.ERROR)  # harmless warnings on many exported PDFs
    return "\n".join(p.extract_text() or "" for p in PdfReader(path).pages)


# Cheapest model: about $0.001 per application. claude-sonnet-5-5 or claude-opus-5-5 write
# better free-text answers at roughly 10x and 30x the cost.
DEFAULT_MODEL = "claude-haiku-5-5"


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
            f"JOB DESCRIPTION (excerpt):\n{(job.get('description') or '')[:3000]}\n\n"
            f"QUESTIONS:\n{json.dumps(qs, indent=1)}")
    schema = {
        "type": "object",
        "properties": {"answers": {"type": "array", "items": {
            "type": "object",
            "properties": {"id": {"type": "string"}, "answer": {"type": "string"}, "confident": {"type": "boolean"}},
            "required": ["id", "answer", "confident"], "additionalProperties": False}}},
        "required": ["answers"], "additionalProperties": False,
    }
    model = answers.get("claude_model", DEFAULT_MODEL)
    kwargs = {}
    if not model.startswith("claude-haiku"):
        # Larger models can re-run a declined request on another model; Haiku has no fallback.
        kwargs = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
    client = anthropic.Anthropic()
    resp = client.beta.messages.create(
        model=model,
        max_tokens=4000,  # caps the cost of any one application
        output_config={"effort": answers.get("claude_effort", "low"),
                       "format": {"type": "json_schema", "schema": schema}},
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
        **kwargs,
    )
    if resp.stop_reason != "end_turn":
        return {}
    text = next((b.text for b in resp.content if b.type == "text"), "{}")
    return {a["id"]: a["answer"] for a in json.loads(text)["answers"] if a["confident"] and a["answer"].strip()}


def key_problems(key: str) -> list[str]:
    """Common ways a pasted API key goes wrong, without printing the key."""
    out = []
    if key != key.strip():
        out.append("it has a space or line break at the start or end")
    k = key.strip()
    if k[:1] in "'\"" or k[-1:] in "'\"":
        out.append("it has quote marks inside it (the quotes in `export ANTHROPIC_API_KEY=\"...\"` must not be part of the key)")
    if not k.strip("'\"").startswith("sk-ant-api"):
        out.append("it doesn't start with sk-ant-api (that's how keys from console.anthropic.com start)")
    if "CHANGE" in k.upper() or "..." in k or "YOUR" in k.upper():
        out.append("it looks like the example text rather than a real key")
    return out


def check_claude(answers: dict) -> str | None:
    """None if Claude can be reached with ANTHROPIC_API_KEY, else what's wrong. Costs nothing."""
    import anthropic
    model = answers.get("claude_model", DEFAULT_MODEL)
    try:
        anthropic.Anthropic().models.retrieve(model)
        return None
    except anthropic.AuthenticationError:
        hints = key_problems(os.environ.get("ANTHROPIC_API_KEY", ""))
        return "Anthropic rejected ANTHROPIC_API_KEY (invalid or deleted key)" + (": " + "; ".join(hints) if hints else "")
    except anthropic.NotFoundError:
        return f"the model {model} isn't available to your key (check claude_model in answers.yaml)"
    except Exception as e:
        return f"{type(e).__name__}: {str(e)[:200]}"


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
            for part in [p for p in answer.split("|") if p.strip()]:
                if not set_combobox(page, box, part.strip()):
                    return False
        return True
    except Exception:
        return False


def open_combobox(page, box) -> list[str]:
    """Open a searchable dropdown and return its option texts (tagged with data-aa-opt)."""
    box.click()
    page.wait_for_timeout(500)
    texts = page.evaluate(OPTIONS_JS)
    if not texts:
        box.press("ArrowDown")
        page.wait_for_timeout(400)
        texts = page.evaluate(OPTIONS_JS)
    return texts


def combobox_choices(page, q: dict) -> list[dict]:
    """The options of a dropdown whose question didn't match a rule, so its options can."""
    try:
        texts = open_combobox(page, page.locator(f"[data-aa='{q['id']}']").first)
        page.keyboard.press("Escape")
        return [{"id": q["id"], "text": t} for t in texts[:60]]
    except Exception:
        return []


def set_combobox(page, box, answer: str) -> bool:
    """Searchable dropdowns (react-select on Greenhouse, Ashby, Lever location). Opens the list and
    picks the best option; if it isn't listed, types the answer to search for it."""
    texts = open_combobox(page, box)
    opt = pick_option(answer, [{"id": str(i), "text": t} for i, t in enumerate(texts)])
    # Not listed: search for it. A place ("Toronto, Ontario, Canada") is searched by its first part,
    # since city lists word it their own way ("Toronto, ON, Canada").
    searches = [answer[:40]] + ([answer.split(",")[0].strip()] if "," in answer else [])
    for term in (searches if not opt and answer != AGREE_ANSWER else []):
        try:
            box.fill(term)
        except Exception:  # a <div role=combobox> can't be filled; type into it instead
            page.keyboard.type(term)
        page.wait_for_timeout(2000)  # city lists load from the server as you type
        texts = page.evaluate(OPTIONS_JS)
        opts = [{"id": str(i), "text": t} for i, t in enumerate(texts)]
        opt = pick_option(answer, opts) or (pick_option(term, opts) if term != answer[:40] else None)
        if opt:
            break
    if not opt:
        try:
            box.fill("")
        except Exception:
            pass
        page.keyboard.press("Escape")
        return False
    page.locator(f"[data-aa-opt='{opt['id']}']").first.click()
    page.wait_for_timeout(300)
    page.keyboard.press("Escape")  # multi-selects keep their list open
    return True


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
    # Only buttons that submit an application, never a careers site's "Apply" or search button.
    for sel in ("#submit_app", "button:has-text('Submit application')", "button:has-text('Submit Application')",
                "input[type=submit][value*='Submit' i]", "button[type=submit]:has-text('Submit')",
                "form:has(input[type=file]) button[type=submit]", "form:has(input[type=file]) input[type=submit]",
                "button:has-text('Submit')"):
        b = page.locator(sel)
        for i in range(b.count()):
            if b.nth(i).is_visible() and b.nth(i).is_enabled():
                b.nth(i).click()
                return True
    return False


def wait_for_result(page, timeout_ms=20000) -> tuple[str, list[str]]:
    """applied | captcha | rejected (the form showed errors) | unconfirmed (no thank-you page),
    plus any error messages the form showed."""
    waited, errors = 0, []
    while waited < timeout_ms:
        page.wait_for_timeout(1000)
        waited += 1000
        try:
            body = page.inner_text("body")
        except Exception:
            continue
        if CONFIRM.search(body) or re.search(r"confirmation|thank", page.url, re.I):
            return "applied", []
        if VERIFY.search(body) or captcha_visible(page):
            return "captcha", []
        try:
            errors = page.evaluate(ERRORS_JS)
        except Exception:
            errors = []
        if errors and waited >= 4000:
            return "rejected", errors
    return "unconfirmed", errors


def open_form(page, row) -> bool:
    """Go to the application form. False if the page has no form (closed job, unusual site)."""
    page.goto(form_url(row), wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(2500)
    # Lever and some Ashby pages show the description first with an Apply button.
    for sel in ("a:has-text('Apply for this job')", "button:has-text('Apply for this Job')", "a:has-text('Apply now')"):
        if page.locator(sel).count() and page.locator(sel).first.is_visible():
            page.locator(sel).first.click()
            page.wait_for_timeout(2000)
            break
    try:
        page.wait_for_selector("input[type=file]", state="attached", timeout=15000)
        return True
    except Exception:
        pass
    # A company careers page that shows the form inside a frame: open the frame itself.
    src = page.evaluate("() => (Array.from(document.querySelectorAll('iframe')).map(f => f.src)"
                        ".find(s => /greenhouse\\.io|lever\\.co|ashbyhq\\.com/.test(s)) || '')")
    if src:
        page.goto(src, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(2500)
        return page.locator("input[type=file]").count() > 0
    return False


def apply_one(page, row, job: dict, cfg: dict, answers: dict, resume_pdf: str, resume_txt: str,
              use_llm: bool, dry_run: bool) -> tuple[str, dict]:
    if not open_form(page, row):
        return "failed", {"error": f"no application form on the page (the job may have closed): {page.url}"}
    if captcha_visible(page):
        return "captcha", {}
    filled = prefill(page, row["source"], cfg["profile"], resume_pdf)
    log = {"prefilled": filled, "answers": {}, "unanswered": []}
    values = {**cfg["profile"], **job}  # for {first_name}, {company} etc. in answers

    def answer(q, text) -> bool:
        if set_field(page, q, text):
            log["answers"][q["label"]] = "agreed" if text == AGREE_ANSWER else text
            return True
        return False

    questions = page.evaluate(COLLECT_JS)
    leftovers = []
    for q in questions:
        text = q["label"] + " " + (q["options"][0]["text"] if q["options"] else "")
        if CONSENT.search(text) and not OPT_IN.search(text):
            # Privacy notices: tick the box, or pick "I agree" / "Yes" / "I acknowledge".
            if q["kind"] == "checkbox" and len(q["options"]) == 1:
                if set_field(page, q, q["options"][0]["text"]):
                    log["answers"][q["label"]] = "checked"
                continue
            if q["kind"] in ("select", "combobox", "radio", "buttons") and answer(q, AGREE_ANSWER):
                continue
        sec = q.get("section", "")
        cands = rule_answers(q["label"], answers, values, use_llm, sec)
        if not cands and q["kind"] == "combobox":
            q["options"] = combobox_choices(page, q)
        if not cands and q["options"]:
            # A vague question ("I identify as:") can still match by its options ("Hispanic or Latino").
            cands = rule_answers(q["label"] + " " + " ".join(o["text"] for o in q["options"]), answers, values, use_llm, sec)
        if cands and q["kind"] in ("select", "combobox", "radio", "checkbox", "buttons"):
            q["tried"] = cands
        if not any(answer(q, c) for c in cands):
            leftovers.append(q)

    if leftovers and use_llm:
        try:
            ai = ask_claude(leftovers, job, resume_txt, answers)
        except Exception as e:
            print(f"    Claude call failed: {type(e).__name__}: {str(e)[:200]}")
            if type(e).__name__ in ("AuthenticationError", "PermissionDeniedError"):
                log["claude_off"] = True
            ai, use_llm = {}, False
        still = []
        for q in leftovers:
            if q["id"] in ai and answer(q, ai[q["id"]]):
                continue
            # Claude unavailable: fall back to the template answers meant for no-key mode.
            fallback = [] if use_llm else rule_answers(q["label"], answers, values, False, q.get("section", ""))
            if not any(answer(q, c) for c in fallback):
                still.append(q)
        leftovers = still

    if leftovers:
        # Answering some fields closes others (ticking "Current role" disables the end date).
        still_open = {(f["label"], f.get("section")) for f in page.evaluate(COLLECT_JS)}
        leftovers = [q for q in leftovers if (q["label"], q.get("section")) in still_open]
    missing = [q["label"] for q in leftovers if q["required"]]
    log["unanswered"] = [q["label"] for q in leftovers]
    if missing:
        log["missing_required"] = missing
        log["missing_detail"] = [{k: q.get(k) for k in ("label", "kind", "options", "tried")}
                                 for q in leftovers if q["required"]]
        return "needs_input", log
    if dry_run:
        return "dry_run", log
    if not click_submit(page):
        log["error"] = "no submit button found"
        return "failed", log
    status, errors = wait_for_result(page)
    if status == "rejected":
        log["error"] = "the site didn't accept the form: " + "; ".join(errors)
        return "failed", log
    if errors:
        log["form_errors"] = errors
    return status, log


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
    else:
        problem = check_claude(answers)
        if problem:
            print(f"Claude is switched off for this run: {problem}\n"
                  "Questions covered by answers.yaml (including its template answers) are still answered.\n"
                  "Run `python3 -m autoapply check-key` for help fixing the key.\n")
            use_llm = False

    left = max(limit - store.applied_today(), 0)
    rows, seen = [], set()
    for r in store.by_status("approved", "queued"):
        if cfg.get("prefs") and not title_matches(html.unescape(r["title"]), cfg["prefs"]):
            # Queued before a title filter was added to config.yaml (e.g. "staff").
            if not dry_run:
                store.set_status(r["key"], "skipped", "title no longer matches title_include/title_exclude")
            continue
        if r["dedupe_key"] in seen:  # same title at the same company, e.g. one posting per city
            if not dry_run:
                store.set_status(r["key"], "skipped", "duplicate of another queued posting")
            continue
        seen.add(r["dedupe_key"])
        rows.append(r)
    rows = rows[:left]
    if not rows:
        print(f"Nothing to apply to (applied today: {store.applied_today()}, cap {limit}). Run `daily` first.")
        return
    shots = os.path.join(here, "logs", date.today().isoformat())
    os.makedirs(shots, exist_ok=True)
    totals = {}
    unanswered_path = os.path.join(here, UNANSWERED)
    new_questions = 0
    print(f"{'Dry run: filling but not submitting' if dry_run else 'Applying'} to {len(rows)} jobs...")
    print("The browser fills in every form by itself. You don't need to type or click anything in it.\n")
    banner = BANNER_JS.replace("MESSAGE", (
        "Automatic dry run: this form is being filled in for you and won't be sent. No need to type here."
        if dry_run else "Applying automatically: this form is being filled in and submitted for you. No need to type here."))

    with sync_playwright() as pw:
        profile_dir = os.path.expanduser(cfg.get("browser_profile_dir", "~/.autoapply-browser"))
        ctx, state = None, {"closed": True}

        def fresh_page():
            # Each job gets its own tab. If the browser was closed or crashed, start a new one,
            # so one bad page can't take down the rest of the run.
            nonlocal ctx
            if not state["closed"]:
                try:
                    return ctx.new_page()
                except Exception:
                    pass
            try:
                ctx and ctx.close()
            except Exception:
                pass
            ctx = pw.chromium.launch_persistent_context(profile_dir, headless=headless)
            state["closed"] = False
            ctx.on("close", lambda *_: state.update(closed=True))
            ctx.add_init_script(banner)
            page = ctx.new_page()
            for p in ctx.pages:
                if p != page:
                    p.close()
            return page

        for i, r in enumerate(rows, 1):
            job = dict(r)
            job["title"] = html.unescape(job["title"])  # older rows were saved with &amp; etc.
            print(f"[{i}/{len(rows)}] {job['company']} | {job['title']}")
            for attempt in (1, 2):
                page = fresh_page()
                try:
                    status, log = apply_one(page, r, job, cfg, answers, resume_pdf, resume_txt, use_llm, dry_run)
                    break
                except Exception as e:
                    status, log = "failed", {"error": f"{type(e).__name__}: {str(e).splitlines()[0]}"}
                    closed = re.search(r"closed|detached", str(e), re.I)
                    if not closed:
                        break
                    if attempt == 1:
                        print("    the browser tab closed before this form was done; trying it once more")
                        try:
                            page.close()
                        except Exception:
                            pass
                    else:
                        log["error"] += " (the browser window closed; leave it open while the tool runs)"
            if log.get("claude_off") and use_llm:
                use_llm = False
                print("    Claude rejected ANTHROPIC_API_KEY, so it is off for the rest of this run. "
                      "Run `python3 -m autoapply check-key`.")
            name = re.sub(r"[^A-Za-z0-9]+", "_", f"{job['company']}_{job['title']}")[:80]
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
            new_questions += record_unanswered(unanswered_path, log.get("missing_detail", []), job["company"])
            if log.get("error"):
                print(f"      {log['error']}")
            if not dry_run:  # a dry run never changes a job's status
                store.set_status(r["key"], status, json.dumps(log))
            try:
                page.close()
            except Exception:
                pass
        try:
            ctx.close()
        except Exception:
            pass
    print(f"\nDone: {totals}. Screenshots in {shots}")
    if new_questions:
        print(f"\n{new_questions} new questions were saved to {unanswered_path}.\n"
              "Answer them once there (open -e unanswered.yaml), then run:  python3 -m autoapply retry && python3 -m autoapply auto")
    elif totals.get("needs_input"):
        print(f"\nSome questions are still unanswered in {unanswered_path}. Fill them in, then run `retry`.")
