"""Open each approved job's application form in a real browser and pre-fill it.

You stay in control: the script fills what it can, then waits while you check
the form, answer the custom questions, and click Submit yourself. Nothing is
submitted without you.
"""
from __future__ import annotations

import os

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


def form_url(row) -> str:
    if row["source"] == "lever":
        return row["apply_url"] or row["url"].rstrip("/") + "/apply"
    if row["source"] == "ashby":
        return row["url"].rstrip("/") + "/application"
    if row["source"] == "greenhouse":
        token = row["key"].split(":")[1]
        return f"https://job-boards.greenhouse.io/{token}/jobs/{row['job_id']}"
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

    with sync_playwright() as pw:
        # A persistent profile keeps cookies, so sites that remember you stay remembered.
        ctx = pw.chromium.launch_persistent_context(
            os.path.expanduser(cfg.get("browser_profile_dir", "~/.autoapply-browser")), headless=False
        )
        page = ctx.new_page()
        for i, r in enumerate(rows, 1):
            url = form_url(r)
            print(f"\n[{i}/{len(rows)}] {r['company']} | {r['title']} | {r['location']}\n    {url}")
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(2500)
                filled = prefill(page, r["source"], cfg["profile"], resume)
                print(f"    pre-filled: {', '.join(filled) or 'nothing (fill it by hand)'}")
            except Exception as e:
                print(f"    could not load or fill: {e}")
            print("    Check the form, answer the remaining questions, and click Submit in the browser.")
            ans = input("    Then type  a = applied,  s = skip,  f = failed,  q = quit  > ").strip().lower()
            if ans == "q":
                break
            status = {"a": "applied", "s": "skipped", "f": "failed"}.get(ans)
            if status:
                store.set_status(r["key"], status)
        ctx.close()
    print(f"\nApplied today: {store.applied_today()}")
