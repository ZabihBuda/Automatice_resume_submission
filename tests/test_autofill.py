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
    # Filled in and confirmed: passes, even though the comments still mention CHANGE_ME.
    text = p.read_text().replace("answer: CHANGE_ME", "answer: Yes").replace("confirmed: false", "confirmed: True")
    p.write_text(text)
    assert autofill.load_answers(str(p))["confirmed"] is True


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


def run_form(page, monkeypatch, name, ai=None, dry_run=False, use_llm=True, profile=PROFILE):
    monkeypatch.setattr(autofill, "form_url", lambda r: f"file://{FORMS}/{name}.html")
    monkeypatch.setattr(autofill, "ask_claude", lambda qs, *a: {q["id"]: ai(q) for q in qs if ai and ai(q)})
    row = {"source": "greenhouse", "key": "greenhouse:acme:1", "job_id": "1"}
    job = {"title": "Data Scientist", "company": "Acme", "location": "NY", "description": ""}
    resume = os.path.join(FORMS, "thanks.html")  # any file works for the upload field
    return autofill.apply_one(page, row, job, {"profile": profile}, answers(), resume, "resume", use_llm, dry_run)


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



def test_react_select_form(page, monkeypatch):
    """The newer Greenhouse form: every dropdown is a react-select combobox."""
    status, log = run_form(page, monkeypatch, "greenhouse_react", use_llm=False,
                           profile=dict(PROFILE, current_company="Initech"))
    assert status == "applied", log
    sent = page.evaluate("window.__submitted")
    assert log["answers"]["Please provide the name of your current (or most recent) company*"] == "Initech"
    assert sent["question_1"]["label"] == "Yes" and sent["question_2"]["label"] == "No"
    assert sent["question_3"]["label"] == "I agree"
    assert sent["question_4"]["label"] == "Canada"
    assert sent["question_5"]["label"] == "No"
    assert sent["question_6"]["label"] == "I don't wish to answer"
    assert sent["question_8"]["label"] == "I don't wish to answer"
    assert [o["label"] for o in sent["question_9"]] == ["I don't wish to answer"]
    assert sent["question_10"]["label"] == "I do not want to answer"


def test_pick_option_variants():
    opts = lambda *t: [{"id": str(i), "text": x} for i, x in enumerate(t)]
    assert autofill.pick_option("No", opts("Not a protected veteran", "No"))["text"] == "No"
    assert autofill.pick_option("No", opts("Yes, I will", "No, I will not"))["text"] == "No, I will not"
    assert autofill.pick_option("Decline to self-identify", opts("Man", "Woman", "I don't wish to answer"))["id"] == "2"
    assert autofill.pick_option(autofill.AGREE_ANSWER, opts("I do not agree", "I agree"))["text"] == "I agree"
    assert autofill.pick_option(autofill.AGREE_ANSWER, opts("Maybe")) is None


def test_bare_yes_and_new_default_rules(tmp_path):
    """`answer: yes` without quotes is read as true by YAML; rules added later still apply."""
    p = tmp_path / "answers.yaml"
    p.write_text("confirmed: true\nrules:\n"
                 "  - match: ['legally authori[sz]ed to work']\n    answer: yes\n"
                 "  - match: ['require.*sponsor']\n    answer: no\n")
    a = autofill.load_answers(str(p))
    assert autofill.rule_answer("Are you legally authorized to work in the US?", a) == "Yes"
    # Not in this file, but in answers.example.yaml; the CHANGE_ME answer comes from the rule above.
    assert autofill.rule_answer("Are you currently eligible to work legally in the United States?", a) == "Yes"
    assert autofill.rule_answer("Do you now or in the future require immigration support?", a) == "No"
    assert autofill.rule_answer("Please select up to 2 ethnicities", a) == "Decline to self-identify"
    # A placeholder with no value is passed over rather than sent as "{current_company}".
    assert autofill.rule_answer("Name of your current (or most recent) company", a, {"current_company": ""}) is None
    assert autofill.rule_answer("Preferred First Name", a, {"first_name": "Sam"}) == "Sam"


def test_groups_reach_claude_with_ids(page, monkeypatch):
    """Radio groups left for Claude used to crash with KeyError: 'id'."""
    seen = []
    def ai(q):
        seen.append(q)
        return {"Why": "I build LLM analytics and want to do it at Acme.", "Will": "No"}.get(q["label"][:4].strip())
    a = answers()
    a["rules"] = [r for r in a["rules"] if "require.*sponsor" not in r["match"]]
    monkeypatch.setattr(autofill, "form_url", lambda r: f"file://{FORMS}/greenhouse.html")
    monkeypatch.setattr(autofill, "ask_claude", lambda qs, *rest: {q["id"]: ai(q) for q in qs if ai(q)})
    row = {"source": "greenhouse", "key": "greenhouse:acme:1", "job_id": "1"}
    job = {"title": "Data Scientist", "company": "Acme", "location": "NY", "description": ""}
    status, log = autofill.apply_one(page, row, job, {"profile": PROFILE}, a, os.path.join(FORMS, "thanks.html"),
                                     "resume", True, False)
    assert status == "applied", log
    assert all("id" in q for q in seen)
    assert log["answers"]["Will you now or in the future require visa sponsorship? *"] == "No"


def test_claude_failure_falls_back_to_templates(page, monkeypatch):
    class AuthenticationError(Exception):
        pass
    def boom(*a):
        raise AuthenticationError("Invalid bearer token")
    monkeypatch.setattr(autofill, "form_url", lambda r: f"file://{FORMS}/greenhouse.html")
    monkeypatch.setattr(autofill, "ask_claude", boom)
    row = {"source": "greenhouse", "key": "greenhouse:acme:1", "job_id": "1"}
    job = {"title": "Data Scientist", "company": "Acme", "location": "NY", "description": ""}
    status, log = autofill.apply_one(page, row, job, {"profile": PROFILE}, answers(),
                                     os.path.join(FORMS, "thanks.html"), "resume", True, False)
    assert status == "applied", log
    assert "role at Acme" in log["answers"]["Why do you want to work at Acme? *"]
    assert log["claude_off"]


def test_rejected_form_is_reported(page):
    page.goto(f"file://{FORMS}/greenhouse_react.html")
    page.wait_for_selector("button[type=submit]")
    assert autofill.click_submit(page)
    status, errors = autofill.wait_for_result(page, timeout_ms=6000)
    assert status == "rejected" and "This field is required." in errors[0]


def test_greenhouse_uses_embed_form():
    row = {"source": "greenhouse", "key": "greenhouse:airbnb:123", "job_id": "123", "apply_url": "", "url": ""}
    assert autofill.form_url(row) == "https://job-boards.greenhouse.io/embed/job_app?for=airbnb&token=123"


def test_key_problems():
    assert autofill.key_problems("sk-ant-api03-abc") == []
    assert any("quote" in p for p in autofill.key_problems('"sk-ant-api03-abc"'))
    assert any("space" in p for p in autofill.key_problems("sk-ant-api03-abc\n"))
    assert any("sk-ant-api" in p for p in autofill.key_problems("abc"))
