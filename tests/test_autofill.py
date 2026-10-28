"""Auto-submit tests against local mock forms (no network, no API key)."""
import glob
import os

import pytest
import yaml

from autoapply import autofill
from autoapply.cli import HERE

FORMS = os.path.join(os.path.dirname(__file__), "forms")
PROFILE = dict(first_name="Test", last_name="User", email="t@example.com", phone="555-0100")


def answers():
    with open(os.path.join(HERE, "answers.example.yaml")) as f:
        a = yaml.safe_load(f)
    for r in a["rules"]:
        if r["answer"] == "CHANGE_ME":
            r["answer"] = "Yes" if "authori" in r["match"][0] else "No"
    return a


def test_pick_option():
    opts = [{"id": "1", "text": "Yes"}, {"id": "2", "text": "No"}, {"id": "3", "text": "Decline to self-identify"}]
    assert autofill.pick_option("yes", opts)["id"] == "1"
    assert autofill.pick_option("Decline to self-identify", opts)["id"] == "3"
    assert autofill.pick_option("Maybe", opts) is None


def test_rules():
    a = answers()
    assert autofill.rule_answer("Are you legally authorized to work in the United States? *", a) == "Yes"
    assert autofill.rule_answer("Will you require visa sponsorship?", a) == "No"
    assert autofill.rule_answer("Veteran status", a) == "I don't wish to answer"
    assert autofill.rule_answer("Do you have open source contributions?", a) is None
    why = autofill.rule_answer("Why do you want to work at Acme? *", a, {"company": "Acme", "title": "Data Scientist"})
    assert "Data Scientist role at Acme" in why and "{" not in why
    assert autofill.rule_answer("Why do you want to work at Acme?", a, use_llm=True) is None


def test_no_key_uses_templates(page, monkeypatch):
    status, log = run_form(page, monkeypatch, "greenhouse", use_llm=False)
    assert status == "applied", log
    assert "role at Acme" in log["answers"]["Why do you want to work at Acme? *"]


def test_refuses_unfinished_answers(tmp_path):
    p = tmp_path / "answers.yaml"
    p.write_text(open(os.path.join(HERE, "answers.example.yaml")).read())
    with pytest.raises(SystemExit, match="CHANGE_ME"):
        autofill.load_answers(str(p))


@pytest.fixture(scope="module")
def page():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as pw:
        exe = (glob.glob("/opt/pw-browsers/chromium-*/chrome-linux*/chrome") or [None])[0]
        try:
            browser = pw.chromium.launch(executable_path=exe) if exe else pw.chromium.launch()
        except Exception as e:
            pytest.skip(f"no browser: {e}")
        yield browser.new_page()
        browser.close()


def run_form(page, monkeypatch, name, ai=None, dry_run=False, use_llm=True):
    monkeypatch.setattr(autofill, "form_url", lambda r: f"file://{FORMS}/{name}.html")
    monkeypatch.setattr(autofill, "ask_claude", lambda qs, *a: {q["id"]: ai(q) for q in qs if ai and ai(q)})
    row = {"source": "greenhouse", "key": "greenhouse:acme:1", "job_id": "1"}
    job = {"title": "Data Scientist", "company": "Acme", "location": "NY", "description": ""}
    resume = os.path.join(FORMS, "thanks.html")  # any file works for the upload field
    return autofill.apply_one(page, row, job, {"profile": PROFILE}, answers(), resume, "resume", use_llm, dry_run)


def why(q):
    return "I build LLM analytics and want to do it at Acme." if q["label"].startswith("Why") else None


def test_fills_and_submits(page, monkeypatch):
    status, log = run_form(page, monkeypatch, "greenhouse", ai=why)
    assert status == "applied", log
    assert log["answers"]["Are you legally authorized to work in the United States? *"] == "Yes"
    assert log["answers"]["Gender"] == "Decline to self-identify"
    assert {"first_name", "email", "resume"} <= set(log["prefilled"])


def test_dry_run_does_not_submit(page, monkeypatch):
    status, _ = run_form(page, monkeypatch, "greenhouse", ai=why, dry_run=True)
    assert status == "dry_run" and "greenhouse.html" in page.url


def test_skips_when_required_answer_unknown(page, monkeypatch):
    status, log = run_form(page, monkeypatch, "hard", ai=why)
    assert status == "needs_input"
    assert log["missing_required"] == ["Describe your experience with Kubernetes in production. *"]
    assert "hard.html" in page.url  # never submitted


def test_skips_captcha(page, monkeypatch):
    status, _ = run_form(page, monkeypatch, "captcha", ai=why)
    assert status == "captcha"
