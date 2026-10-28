"""Decide which jobs fit, and rank them against the resume."""
from __future__ import annotations

import re

from .sources import Job

US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL", "IN", "IA",
    "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT",
    "VA", "WA", "WV", "WI", "WY", "DC",
}
US_WORDS = [
    "united states", "usa", "u.s.", "us-remote", "remote us", "remote - us", "us remote",
    "new york", "san francisco", "seattle", "boston", "austin", "chicago", "los angeles",
    "denver", "atlanta", "washington", "bay area", "palo alto", "mountain view", "menlo park",
    "san jose", "san diego", "miami", "philadelphia", "pittsburgh", "dallas", "houston",
    "salt lake", "portland", "minneapolis", "detroit", "raleigh", "nashville", "phoenix",
    "sunnyvale", "syracuse", "virginia", "redwood city", "cambridge, ma", "brooklyn", "jersey city", "north america",
]
NON_US_WORDS = [
    "canada", "toronto", "vancouver", "montreal", "london", "uk", "united kingdom", "dublin",
    "ireland", "germany", "berlin", "india", "bangalore", "bengaluru", "hyderabad", "singapore",
    "australia", "sydney", "brazil", "mexico", "poland", "spain", "france", "paris",
    "amsterdam", "netherlands", "japan", "tokyo", "israel", "tel aviv", "emea", "apac", "latam",
]
# Phrases that mean the role is closed to a TN-visa Canadian citizen.
BLOCKING_PHRASES = [
    "u.s. citizen", "us citizen", "united states citizen", "citizenship is required",
    "security clearance", "active clearance", "ts/sci", "secret clearance", "green card holder",
]
# Phrases worth a human look (TN does not need H-1B sponsorship, but some
# employers refuse any visa paperwork at all).
WARNING_PHRASES = [
    "not able to sponsor", "unable to sponsor", "will not sponsor", "cannot sponsor",
    "no sponsorship", "without sponsorship", "without the need for sponsorship",
    "authorized to work in the united states without",
]


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower()).strip()


def is_us_location(job: Job) -> bool:
    loc = _norm(job.location)
    country = _norm(job.extra.get("country", ""))
    if country in {"us", "usa", "united states"}:
        return True
    if country and country not in {"us", "usa", "united states"}:
        # Ashby/Lever gave an explicit non-US country; trust it unless remote-US is named.
        if not any(w in loc for w in ("united states", "usa", "us remote", "remote us")):
            return False
    if any(w in loc for w in US_WORDS):
        return True
    # "City, CA" style
    if any(re.search(rf",\s*{st}\b", job.location or "") for st in US_STATES):
        return True
    if any(w in loc for w in NON_US_WORDS):
        return False
    # Bare "Remote" with no region: keep it and let the description decide.
    return loc in {"remote", "anywhere", ""}


def is_preferred_location(job: Job, prefs: dict) -> bool:
    return any(re.search(p, job.location or "", re.I) for p in prefs.get("preferred_locations", []))


def title_matches(title: str, prefs: dict) -> bool:
    t = _norm(title)
    if not any(k in t for k in prefs["title_include"]):
        return False
    return not any(re.search(rf"\b{re.escape(k)}\b", t) for k in prefs["title_exclude"])


def visa_flags(job: Job) -> tuple[bool, list[str]]:
    """Returns (blocked, warnings)."""
    d = _norm(job.description)
    blocked = any(p in d for p in BLOCKING_PHRASES)
    warns = [p for p in WARNING_PHRASES if p in d]
    return blocked, warns


def score(job: Job, prefs: dict) -> float:
    """Keyword overlap with the resume's skills, plus a few preference bonuses."""
    text = _norm(job.title + " " + job.description)
    hits = [k for k in prefs["skills"] if re.search(rf"\b{re.escape(k.lower())}\b", text)]
    s = len(hits) / max(len(prefs["skills"]), 1) * 100
    t = _norm(job.title)
    s += sum(10 for k in prefs.get("title_bonus", []) if k in t)
    if job.remote and prefs.get("prefer_remote"):
        s += 5
    if is_preferred_location(job, prefs):
        s += prefs.get("preferred_location_bonus", 15)
    return round(s, 1)


def evaluate(job: Job, prefs: dict) -> dict | None:
    """None if the job is filtered out, else a dict of scoring details."""
    if not title_matches(job.title, prefs):
        return None
    if prefs.get("us_only", True) and not is_us_location(job):
        return None
    if prefs.get("remote_only") and not job.remote:
        return None
    blocked, warns = visa_flags(job)
    if blocked:
        return None
    return {"score": score(job, prefs), "warnings": warns}
