"""Command line entry point.

    python -m autoapply daily     # fetch new jobs + build today's queue of 30 (put this on a schedule)
    python -m autoapply review    # approve or skip each queued job
    python -m autoapply apply     # pre-fill approved applications in a browser; you click Submit
    python -m autoapply log       # what has been applied to, and totals
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import os
import webbrowser
from datetime import date

import yaml

from . import apply as apply_mod
from .db import Store
from .filters import evaluate
from .sources import fetch_all

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_config(path: str) -> dict:
    with open(path) as f:
        cfg = yaml.safe_load(f)
    with open(os.path.join(HERE, cfg.get("companies_file", "companies.yaml"))) as f:
        cfg["companies"] = yaml.safe_load(f)["companies"]
    cfg["prefs"]["title_include"] = [s.lower() for s in cfg["prefs"]["title_include"]]
    cfg["prefs"]["title_exclude"] = [s.lower() for s in cfg["prefs"]["title_exclude"]]
    return cfg


def cmd_fetch(store: Store, cfg: dict):
    print(f"Fetching {len(cfg['companies'])} job boards...")
    jobs = fetch_all(cfg["companies"])
    kept = new = 0
    for j in jobs:
        ev = evaluate(j, cfg["prefs"])
        if ev is None:
            continue
        kept += 1
        new += store.upsert(j, ev["score"], ev["warnings"])
    print(f"Saw {len(jobs)} jobs, {kept} match your filters, {new} are new.")


def cmd_queue(store: Store, cfg: dict, limit: int):
    day = date.today().isoformat()
    picked = store.pick_for_queue(limit, cfg["prefs"].get("max_per_company", 3))
    store.mark_queued([r["key"] for r in picked], day)
    out = os.path.join(HERE, "queue")
    os.makedirs(out, exist_ok=True)
    rows = store.by_status("queued")
    with open(os.path.join(out, f"{day}.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["score", "company", "title", "location", "remote", "warnings", "url", "key"])
        for r in rows:
            w.writerow([r["score"], r["company"], r["title"], r["location"], r["remote"],
                        "; ".join(json.loads(r["warnings"] or "[]")), r["url"], r["key"]])
    path = os.path.join(out, f"{day}.html")
    with open(path, "w") as f:
        f.write(render_html(rows, day))
    print(f"Queued {len(picked)} new jobs ({len(rows)} waiting for review). Report: {path}")
    return path


def render_html(rows, day) -> str:
    trs = []
    for r in rows:
        warn = "; ".join(json.loads(r["warnings"] or "[]"))
        trs.append(
            f"<tr><td>{r['score']}</td><td>{html.escape(r['company'])}</td>"
            f"<td><a href='{html.escape(r['url'])}' target=_blank>{html.escape(r['title'])}</a></td>"
            f"<td>{html.escape(r['location'] or '')}</td><td>{'yes' if r['remote'] else ''}</td>"
            f"<td class=w>{html.escape(warn)}</td></tr>"
        )
    return f"""<!doctype html><meta charset=utf-8><title>Job queue {day}</title>
<style>body{{font:14px system-ui;margin:24px}}table{{border-collapse:collapse;width:100%}}
td,th{{border-bottom:1px solid #ddd;padding:6px;text-align:left}}.w{{color:#b45309}}</style>
<h2>Job queue for {day} ({len(rows)} to review)</h2>
<p>Run <code>python -m autoapply review</code> to approve or skip. Orange text means the posting mentions sponsorship; TN status usually still works, but check.</p>
<table><tr><th>Score</th><th>Company</th><th>Title</th><th>Location</th><th>Remote</th><th>Visa note</th></tr>
{''.join(trs)}</table>"""


def cmd_review(store: Store, approve_all: bool):
    rows = store.by_status("queued")
    if not rows:
        print("Nothing waiting for review. Run `python -m autoapply daily` first.")
        return
    for i, r in enumerate(rows, 1):
        if approve_all:
            store.set_status(r["key"], "approved")
            continue
        warn = "; ".join(json.loads(r["warnings"] or "[]"))
        print(f"\n[{i}/{len(rows)}] score {r['score']}  {r['company']} | {r['title']}\n    {r['location']}  {r['url']}")
        if warn:
            print(f"    visa note: {warn}")
        ans = input("    y = approve, n = skip, o = open in browser, q = stop > ").strip().lower()
        if ans == "o":
            webbrowser.open(r["url"])
            ans = input("    y / n / q > ").strip().lower()
        if ans == "q":
            break
        if ans in ("y", "n"):
            store.set_status(r["key"], "approved" if ans == "y" else "skipped")
    print(f"\nApproved and waiting to apply: {len(store.by_status('approved'))}")


def cmd_log(store: Store):
    for r in store.by_status("applied", "failed"):
        print(f"{(r['applied_at'] or '')[:10]}  {r['status']:8} {r['company']:<20} {r['title']}")
    print("\nTotals:", store.counts())


def main(argv=None):
    ap = argparse.ArgumentParser(prog="autoapply")
    ap.add_argument("--config", default=os.path.join(HERE, "config.yaml"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("fetch")
    q = sub.add_parser("queue"); q.add_argument("--limit", type=int)
    d = sub.add_parser("daily"); d.add_argument("--limit", type=int); d.add_argument("--open", action="store_true")
    rv = sub.add_parser("review"); rv.add_argument("--approve-all", action="store_true")
    a = sub.add_parser("apply"); a.add_argument("--limit", type=int)
    sub.add_parser("log")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    store = Store(os.path.join(HERE, cfg.get("db_path", "applications.db")))
    limit = getattr(args, "limit", None) or cfg["prefs"].get("daily_limit", 30)

    if args.cmd == "fetch":
        cmd_fetch(store, cfg)
    elif args.cmd == "queue":
        cmd_queue(store, cfg, limit)
    elif args.cmd == "daily":
        cmd_fetch(store, cfg)
        path = cmd_queue(store, cfg, limit)
        if args.open:
            webbrowser.open("file://" + path)
    elif args.cmd == "review":
        cmd_review(store, args.approve_all)
    elif args.cmd == "apply":
        apply_mod.run(store, cfg, limit)
    elif args.cmd == "log":
        cmd_log(store)
