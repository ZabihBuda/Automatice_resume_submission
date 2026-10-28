"""Fetch jobs from public applicant-tracking-system (ATS) job board APIs.

Greenhouse, Lever and Ashby publish read-only JSON feeds of each company's
open roles. They need no login and are meant for exactly this kind of use,
so fetching them carries no account-ban risk.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

import requests

TIMEOUT = 20
HEADERS = {"User-Agent": "personal-job-search/0.1 (single user, once a day)"}


@dataclass
class Job:
    source: str          # greenhouse | lever | ashby
    company: str
    job_id: str
    title: str
    location: str
    url: str             # public posting page
    apply_url: str       # page holding the application form
    description: str = ""
    remote: bool = False
    posted_at: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.source}:{self.company}:{self.job_id}"


def _strip_html(text: str) -> str:
    text = html.unescape(text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def fetch_greenhouse(token: str, name: str | None = None) -> list[Job]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
    data = requests.get(url, headers=HEADERS, timeout=TIMEOUT).json()
    jobs = []
    for j in data.get("jobs", []):
        loc = (j.get("location") or {}).get("name", "")
        jobs.append(Job(
            source="greenhouse", company=name or token, job_id=str(j["id"]),
            title=j.get("title", ""), location=loc,
            url=j.get("absolute_url", ""), apply_url=j.get("absolute_url", ""),
            description=_strip_html(j.get("content", "")),
            remote="remote" in loc.lower(), posted_at=j.get("updated_at", ""),
            extra={"board_token": token},
        ))
    return jobs


def fetch_lever(token: str, name: str | None = None) -> list[Job]:
    url = f"https://api.lever.co/v0/postings/{token}?mode=json"
    data = requests.get(url, headers=HEADERS, timeout=TIMEOUT).json()
    jobs = []
    for j in data if isinstance(data, list) else []:
        cats = j.get("categories") or {}
        loc = cats.get("location", "") or ", ".join(cats.get("allLocations") or [])
        workplace = (j.get("workplaceType") or "").lower()
        jobs.append(Job(
            source="lever", company=name or token, job_id=j["id"],
            title=j.get("text", ""), location=loc,
            url=j.get("hostedUrl", ""), apply_url=j.get("applyUrl", ""),
            description=_strip_html(j.get("descriptionPlain") or j.get("description", "")),
            remote=workplace == "remote" or "remote" in loc.lower(),
            posted_at=str(j.get("createdAt", "")),
            extra={"country": j.get("country", "")},
        ))
    return jobs


def fetch_ashby(token: str, name: str | None = None) -> list[Job]:
    url = f"https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true"
    data = requests.get(url, headers=HEADERS, timeout=TIMEOUT).json()
    jobs = []
    for j in data.get("jobs", []):
        if not j.get("isListed", True):
            continue
        locs = [j.get("location", "")] + [s.get("location", "") for s in j.get("secondaryLocations") or []]
        addr = ((j.get("address") or {}).get("postalAddress") or {})
        jobs.append(Job(
            source="ashby", company=name or token, job_id=j["id"],
            title=j.get("title", ""), location="; ".join(l for l in locs if l),
            url=j.get("jobUrl", ""), apply_url=j.get("applyUrl") or j.get("jobUrl", ""),
            description=_strip_html(j.get("descriptionPlain") or j.get("descriptionHtml", "")),
            remote=bool(j.get("isRemote")), posted_at=j.get("publishedAt", ""),
            extra={"country": addr.get("addressCountry", "")},
        ))
    return jobs


FETCHERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby}


def fetch_all(companies: list[dict], log=print) -> list[Job]:
    """companies: [{"ats": "greenhouse", "token": "airbnb", "name": "Airbnb"}, ...]"""
    jobs: list[Job] = []
    for c in companies:
        fetcher = FETCHERS.get(c["ats"])
        if not fetcher:
            log(f"  ! unknown ATS '{c['ats']}' for {c['token']}")
            continue
        try:
            got = fetcher(c["token"], c.get("name"))
            jobs.extend(got)
        except Exception as e:  # one dead board must not stop the run
            log(f"  ! {c['ats']}/{c['token']}: {type(e).__name__}: {e}")
    return jobs
