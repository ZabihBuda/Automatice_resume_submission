"""Open each approved job's application form in a real browser and pre-fill it.

You stay in control: the script fills what it can, then waits while you check
the form, answer the custom questions, and click Submit yourself. Nothing is
submitted without you.
"""
from __future__ import annotations

import os
import re
from collections import deque

import yaml

from .db import Store

# Field label (lowercased substring) -> profile key. Used on every ATS as a fallback.
LABEL_MAP = [
    ("first name", "first_name"), ("last name", "last_name"), ("full name", "full_name"),
    ("preferred name", "first_name"), ("email", "email"), ("phone", "phone"),
    ("linkedin", "linkedin"), ("github", "github"), ("website", "website"),
    ("portfolio", "website"), ("current company", "current_company"),
    ("current employer", "current_company"), ("current location", "location"),
    ("location", "location"), ("city", "city"),
]

# ATS-specific selectors, tried first.
SELECTORS = {
    "greenhouse": {
        "first_name": "#first_name", "last_name": "#last_name", "email": "#email", "phone": "#phone",
        "resume": "input[type=file]#resume, input[type=file][id*=resume], input[type=file]",
    },
    "lever": {
        "full_name": "input[name=name]", "email": "input[name=email]", "phone": "input[name=phone]",
        "current_company": "input[name=org]", "linkedin": "input[name='urls[LinkedIn]']",
        "github": "input[name='urls[GitHub]']", "website": "input[name='urls[Portfolio]']",
        "resume": "input[type=file][name=resume], input[type=file]",
    },
    "ashby": {
        "full_name": "#_systemfield_name", "email": "#_systemfield_email", "phone": "#_systemfield_phone",
        "resume": "input[type=file]#_systemfield_resume, input[type=file]",
    },
}


def _profile_values(p: dict) -> dict:
    v = dict(p)
    v.setdefault("full_name", f"{p['first_name']} {p['last_name']}")
    return v


def _fill_if_empty(locator, value: str) -> bool:
    try:
        if locator.count() and locator.first.is_visible() and not locator.first.input_value():
            locator.first.fill(value)
            return True
    except Exception:
        pass
    return False


def prefill(page, source: str, profile: dict, resume_path: str) -> list[str]:
    vals = _profile_values(profile)
    filled = []
    for key, sel in SELECTORS.get(source, {}).items():
        if key == "resume":
            try:
                page.locator(sel).first.set_input_files(resume_path)
                filled.append("resume")
            except Exception:
                pass
        elif vals.get(key) and _fill_if_empty(page.locator(sel), vals[key]):
            filled.append(key)
    for label, key in LABEL_MAP:
        if key in filled or not vals.get(key):
            continue
        if _fill_if_empty(page.get_by_label(label, exact=False), vals[key]):
            filled.append(key)
    return filled


def greenhouse_token(row) -> str:
    """The company's job-board id ("doordashusa"), which the job key doesn't hold (it has "DoorDash")."""
    m = re.search(r"greenhouse\.io/([\w-]+)/jobs/", row["url"] or "")
    if m:
        return m.group(1)
    company = row["key"].split(":")[1]
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "companies.yaml")
    try:
        with open(path) as f:
            boards = yaml.safe_load(f)
        boards = boards.get("companies", boards) if isinstance(boards, dict) else boards
        for c in boards:
            if c.get("ats") == "greenhouse" and company in (c.get("name"), c.get("token")):
                return c["token"]
    except (OSError, AttributeError, TypeError):
        pass
    return company


def form_url(row) -> str:
    if row["source"] == "lever":
        return row["apply_url"] or row["url"].rstrip("/") + "/apply"
    if row["source"] == "ashby":
        return row["url"].rstrip("/") + "/application"
    if row["source"] == "greenhouse":
        # The embeddable form: the plain job page sends companies like Airbnb, Stripe and DoorDash
        # to their own careers site, where the form is hidden in a frame or behind more clicks.
        return f"https://job-boards.greenhouse.io/embed/job_app?for={greenhouse_token(row)}&token={row['job_id']}"
    return row["apply_url"] or row["url"]


def run(store: Store, cfg: dict, limit: int):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SystemExit("Playwright is not installed. Run: pip install playwright && playwright install chromium")

    resume = os.path.expanduser(cfg["profile"]["resume_path"])
    if not os.path.exists(resume):
        raise SystemExit(f"Resume not found at {resume}. Set profile.resume_path in config.yaml.")

    done_today = store.applied_today()
    rows = store.by_status("approved")[: max(limit - done_today, 0)]
    if not rows:
        print(f"Nothing to do: {len(store.by_status('approved'))} approved, {done_today} applied today (cap {limit}).")
        return

    tabs_at_once = max(int(cfg.get("tabs_at_once", 5)), 1)
    print(f"{len(rows)} approved applications. Opening {min(tabs_at_once, len(rows))} at a time in browser tabs.")
    print("After you click Submit on a tab, come back to THIS window and type a, s or f.\n")

    with sync_playwright() as pw:
        # A persistent profile keeps cookies, so sites that remember you stay remembered.
        ctx = pw.chromium.launch_persistent_context(
            os.path.expanduser(cfg.get("browser_profile_dir", "~/.autoapply-browser")), headless=False
        )
        pending, open_tabs = deque(rows), deque()

        def top_up():
            while pending and len(open_tabs) < tabs_at_once:
                r = pending.popleft()
                page = ctx.new_page()
                try:
                    page.goto(form_url(r), wait_until="domcontentloaded", timeout=45000)
                    page.wait_for_timeout(2500)
                    filled = prefill(page, r["source"], cfg["profile"], resume)
                    note = f"pre-filled: {', '.join(filled) or 'nothing (fill it by hand)'}"
                except Exception as e:
                    note = f"could not load or fill: {e}"
                open_tabs.append((page, r, note))

        top_up()
        for blank in [p for p in ctx.pages if p.url == "about:blank"]:
            blank.close()
        i = 0
        while open_tabs:
            page, r, note = open_tabs.popleft()
            i += 1
            try:
                page.bring_to_front()
            except Exception:
                pass
            print(f"\a\n[{i}/{len(rows)}] {r['company']} | {r['title']} | {r['location']}\n    {form_url(r)}\n    {note}")
            print("    This tab is now in front. Answer the remaining questions and click Submit.")
            ans = input("    Then type  a = applied,  s = skip,  f = failed,  q = quit  > ").strip().lower()
            if ans == "q":
                break
            status = {"a": "applied", "s": "skipped", "f": "failed"}.get(ans)
            if status:
                store.set_status(r["key"], status)
            try:
                page.close()
            except Exception:
                pass
            top_up()
        ctx.close()
    print(f"\nApplied today: {store.applied_today()}")
