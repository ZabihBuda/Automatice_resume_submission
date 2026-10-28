"""SQLite store: every job ever seen, plus its status. This is the dedupe and the application log."""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone

from .sources import Job

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    key         TEXT PRIMARY KEY,           -- source:company:job_id
    dedupe_key  TEXT NOT NULL,              -- company + normalized title, catches reposts
    source      TEXT, company TEXT, job_id TEXT, title TEXT, location TEXT,
    url TEXT, apply_url TEXT, remote INTEGER, posted_at TEXT,
    score REAL, warnings TEXT, description TEXT,
    status      TEXT NOT NULL DEFAULT 'new', -- new | queued | approved | skipped | applied | failed
                                         -- | needs_input | captcha | unconfirmed
    first_seen  TEXT, queued_on TEXT, applied_at TEXT, notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_dedupe ON jobs(dedupe_key);
CREATE INDEX IF NOT EXISTS idx_status ON jobs(status);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def dedupe_key(company: str, title: str) -> str:
    t = re.sub(r"[^a-z0-9 ]", " ", title.lower())
    t = re.sub(r"\b(i|ii|iii|iv|1|2|3)\b", " ", t)
    return company.lower() + "|" + " ".join(t.split())


class Store:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(jobs)")}
        if "description" not in cols:  # databases created before auto-submit
            self.conn.execute("ALTER TABLE jobs ADD COLUMN description TEXT")

    def upsert(self, job: Job, score: float, warnings: list[str]) -> bool:
        """Insert a new job. Returns False if it, or a same-company same-title job, is already known."""
        if self.conn.execute("SELECT 1 FROM jobs WHERE key=?", (job.key,)).fetchone():
            return False
        dk = dedupe_key(job.company, job.title)
        dup = self.conn.execute(
            "SELECT 1 FROM jobs WHERE dedupe_key=? AND status IN ('queued','approved','applied')", (dk,)
        ).fetchone()
        self.conn.execute(
            "INSERT INTO jobs (key, dedupe_key, source, company, job_id, title, location, url, apply_url,"
            " remote, posted_at, score, warnings, description, status, first_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (job.key, dk, job.source, job.company, job.job_id, job.title, job.location, job.url,
             job.apply_url, int(job.remote), job.posted_at, score, json.dumps(warnings), job.description,
             "skipped" if dup else "new", now()),
        )
        self.conn.commit()
        return not dup

    def pick_for_queue(self, limit: int, max_per_company: int) -> list[sqlite3.Row]:
        rows = self.conn.execute("SELECT * FROM jobs WHERE status='new' ORDER BY score DESC").fetchall()
        # Jobs already waiting or applied to count too, so reposts and per-city copies of the
        # same role aren't queued again on later days.
        active = self.conn.execute(
            "SELECT company, dedupe_key, status FROM jobs WHERE status IN ('queued','approved','applied','unconfirmed')"
        ).fetchall()
        picked, per_co = [], {}
        seen = {a["dedupe_key"] for a in active}
        for a in active:
            if a["status"] in ("queued", "approved"):
                per_co[a["company"]] = per_co.get(a["company"], 0) + 1
        for r in rows:
            if per_co.get(r["company"], 0) >= max_per_company or r["dedupe_key"] in seen:
                continue
            picked.append(r)
            per_co[r["company"]] = per_co.get(r["company"], 0) + 1
            seen.add(r["dedupe_key"])
            if len(picked) >= limit:
                break
        return picked

    def set_status(self, key: str, status: str, notes: str | None = None):
        submitted = status in ("applied", "unconfirmed")
        extra = ", applied_at=?" if submitted else ""
        params = [status, notes] + ([now()] if submitted else []) + [key]
        self.conn.execute(f"UPDATE jobs SET status=?, notes=COALESCE(?, notes){extra} WHERE key=?", params)
        self.conn.commit()

    def mark_queued(self, keys: list[str], day: str):
        self.conn.executemany("UPDATE jobs SET status='queued', queued_on=? WHERE key=?", [(day, k) for k in keys])
        self.conn.commit()

    def by_status(self, *statuses: str) -> list[sqlite3.Row]:
        q = ",".join("?" * len(statuses))
        return self.conn.execute(f"SELECT * FROM jobs WHERE status IN ({q}) ORDER BY score DESC", statuses).fetchall()

    def requeue(self, *statuses: str) -> int:
        q = ",".join("?" * len(statuses))
        n = self.conn.execute(f"UPDATE jobs SET status='approved' WHERE status IN ({q})", statuses).rowcount
        self.conn.commit()
        return n

    def counts(self) -> dict:
        return dict(self.conn.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status").fetchall())

    def applied_today(self) -> int:
        today = datetime.now(timezone.utc).date().isoformat()
        return self.conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE status IN ('applied','unconfirmed') AND applied_at LIKE ?", (today + "%",)
        ).fetchone()[0]
