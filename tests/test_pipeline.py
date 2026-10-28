"""Offline tests: fake job board responses, run filters, dedupe and queueing."""
import os
import tempfile

import yaml

from autoapply import sources
from autoapply.cli import HERE
from autoapply.db import Store
from autoapply.filters import evaluate, is_us_location
from autoapply.sources import Job

GH = {"jobs": [
    {"id": 1, "title": "Senior Data Scientist", "location": {"name": "New York, NY"},
     "absolute_url": "https://x/1", "content": "&lt;p&gt;Python, SQL, LLM, RAG, Spark&lt;/p&gt;"},
    {"id": 2, "title": "Data Scientist Intern", "location": {"name": "Remote - US"},
     "absolute_url": "https://x/2", "content": "python"},
    {"id": 3, "title": "Machine Learning Engineer", "location": {"name": "London, UK"},
     "absolute_url": "https://x/3", "content": "python"},
    {"id": 4, "title": "Data Scientist, Defense", "location": {"name": "Washington, DC"},
     "absolute_url": "https://x/4", "content": "Must be a U.S. citizen with active clearance"},
    {"id": 5, "title": "Data Scientist II", "location": {"name": "Remote"},
     "absolute_url": "https://x/5", "content": "We are unable to sponsor visas. python sql"},
    {"id": 6, "title": "Account Executive", "location": {"name": "Austin, TX"},
     "absolute_url": "https://x/6", "content": ""},
]}


class FakeResp:
    def __init__(self, data): self.data = data
    def json(self): return self.data


def prefs():
    with open(os.path.join(HERE, "config.example.yaml")) as f:
        p = yaml.safe_load(f)["prefs"]
    p["title_include"] = [s.lower() for s in p["title_include"]]
    p["title_exclude"] = [s.lower() for s in p["title_exclude"]]
    return p


def test_greenhouse_parse_and_filter(monkeypatch):
    monkeypatch.setattr(sources.requests, "get", lambda *a, **k: FakeResp(GH))
    jobs = sources.fetch_greenhouse("acme", "Acme")
    assert len(jobs) == 6 and "LLM" in jobs[0].description
    kept = {j.job_id: evaluate(j, prefs()) for j in jobs}
    assert kept["1"] and kept["1"]["score"] > kept["5"]["score"]
    assert kept["2"] is None   # intern
    assert kept["3"] is None   # London
    assert kept["4"] is None   # citizenship / clearance
    assert kept["5"]["warnings"] == ["unable to sponsor"]
    assert kept["6"] is None   # not a DS title


def test_location_rules():
    mk = lambda loc, country="": Job("lever", "c", "1", "t", loc, "", "", extra={"country": country})
    assert is_us_location(mk("San Francisco, CA"))
    assert is_us_location(mk("Remote", "US"))
    assert not is_us_location(mk("Toronto, Canada"))
    assert not is_us_location(mk("Remote", "CA"))


def test_dedupe_and_queue():
    with tempfile.TemporaryDirectory() as d:
        s = Store(os.path.join(d, "t.db"))
        mk = lambda i, co, title, sc: (Job("greenhouse", co, str(i), title, "NY, NY", "u", "u"), sc)
        for job, sc in [mk(1, "A", "Data Scientist", 50), mk(2, "A", "Data Scientist", 40),
                        mk(3, "A", "ML Engineer", 30), mk(4, "B", "Data Scientist", 20)]:
            s.upsert(job, sc, [])
        assert not s.upsert(mk(1, "A", "Data Scientist", 50)[0], 50, [])   # same id again
        picked = s.pick_for_queue(limit=30, max_per_company=1)
        assert [r["job_id"] for r in picked] == ["1", "4"]  # one per company, best first
        s.mark_queued([r["key"] for r in picked], "2026-10-27")
        # A repost with the same title at the same company is skipped automatically.
        assert not s.upsert(Job("greenhouse", "A", "9", "Data Scientist", "NY, NY", "u", "u"), 99, [])
        s.set_status(picked[0]["key"], "applied")
        assert s.applied_today() == 1


def test_preferred_locations():
    mk = lambda loc: Job("greenhouse", "c", "1", "Data Scientist", loc, "", "", description="python sql")
    p = prefs()
    base = evaluate(mk("Austin, TX"), p)["score"]
    for loc in ["Syracuse, NY", "McLean, VA", "Arlington, Virginia"]:
        assert evaluate(mk(loc), p)["score"] == base + 15, loc
    assert evaluate(mk("Charleston, West Virginia"), p)["score"] == base
