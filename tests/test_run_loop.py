"""The run loop keeps going when the browser dies on one job."""
import glob
import os

import pytest
import yaml

from autoapply import autofill
from test_autofill import FORMS, PROFILE, answers

def test_run_survives_closed_browser(tmp_path, monkeypatch):
    """If the browser dies on one job, the next job gets a new browser."""
    pytest.importorskip("playwright.sync_api")
    exe = (glob.glob("/opt/pw-browsers/chromium-*/chrome-linux*/chrome") or [None])[0]
    from playwright._impl._browser_type import BrowserType
    orig = BrowserType.launch_persistent_context
    async def launch(self, *a, **k):
        if exe:
            k["executablePath"] = exe
        return await orig(self, *a, **k)
    monkeypatch.setattr(BrowserType, "launch_persistent_context", launch)

    from autoapply.db import Store
    from autoapply.sources import Job
    s = Store(str(tmp_path / "t.db"))
    for i in range(3):
        s.upsert(Job("greenhouse", f"Co{i}", str(i), f"Data Scientist {'ABC'[i]}", "NY, NY", "u", "u"), 50 - i, [])
    for r in s.by_status("new"):
        s.set_status(r["key"], "queued")
    a = answers(); a["confirmed"] = True
    (tmp_path / "answers.yaml").write_text(yaml.safe_dump(a))

    calls = []
    def fake_apply_one(page, row, *rest):
        calls.append(row["job_id"])
        if row["job_id"] == "0":
            page.context.close()  # simulate the window being closed mid-job
            raise RuntimeError("Target page, context or browser has been closed")
        page.goto(f"file://{FORMS}/thanks.html")
        return "applied", {}
    monkeypatch.setattr(autofill, "apply_one", fake_apply_one)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg = {"profile": dict(PROFILE, resume_path=os.path.join(FORMS, "thanks.html")),
           "browser_profile_dir": str(tmp_path / "prof")}
    monkeypatch.setattr(autofill, "resume_text", lambda p: "resume")
    autofill.run(s, cfg, 30, False, True, str(tmp_path))
    assert calls == ["0", "0", "1", "2"]  # the closed job is tried once more in a new window
    assert s.counts() == {"failed": 1, "applied": 2}
