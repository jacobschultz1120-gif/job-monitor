"""
job_monitor.py — Territory Job Monitor  (JSearch edition)
────────────────────────────────────────────────────────────────────────────
Architecture:
  Fast layer (every 5 minutes — free sources):
    • The Muse        (finance/accounting category, no key required)
    • Remotive        (remote finance roles, no key required)
    • Direct ATS      (Greenhouse / Lever / Ashby — real-time, free)

  Broad market layer (2x per day — JSearch API, $10/month):
    • JSearch via RapidAPI — aggregates LinkedIn, Indeed, Glassdoor,
      ZipRecruiter, CareerBuilder, Monster, and 25+ other boards.
      Runs at morning and evening sweeps to stay within Basic plan limits.

  For every candidate from any source:
    1. Fast pre-filter: title match + ERP signal phrase in JD
    2. Claude reads full JD → determines ICP industry, HQ territory,
       buying signal strength, and whether this is a replacement signal
    3. Route to #urgent (8–10) or #watch-list (6–7) in Discord
    4. Suppress if Claude flags wrong industry or outside territory

No static company list required. JSearch returns company website
directly for most postings — no inference needed.
"""

import os
import re
import sys
import json
import time
import logging
from datetime import datetime, timezone, timedelta

import requests
import anthropic

from config import (
    POLL_INTERVAL_SECONDS,
    URGENT_THRESHOLD, WATCHLIST_MIN,
    TARGET_TITLES,
    URGENT_SIGNAL_PHRASES, WATCHLIST_SIGNAL_PHRASES, ALL_SIGNAL_PHRASES,
    ICP_INDUSTRIES, TERRITORY_STATES, TERRITORY_CANADA,
    JSEARCH_SWEEP_TIMES_UTC, JSEARCH_QUERIES,
    JSEARCH_RESULTS_PER_PAGE, JSEARCH_DATE_POSTED,
    GREENHOUSE_COMPANIES, LEVER_COMPANIES, ASHBY_COMPANIES,
    HEARTBEAT_HOUR_UTC, HEARTBEAT_MINUTE_UTC,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

JSEARCH_BASE_URL = "https://jsearch.p.rapidapi.com/search"
JSEARCH_HOST     = "jsearch.p.rapidapi.com"

_last_jsearch_sweep: datetime | None = None


# ─────────────────────────────────────────────────────────────────────────────
# DEDUPLICATION  (file-backed — survives Railway restarts)
# Seen job IDs are written to disk so they persist across restarts/deploys.
# Entries older than 7 days are pruned automatically on each load.
# ─────────────────────────────────────────────────────────────────────────────

SEEN_FILE    = "/tmp/job_monitor_seen.json"
_SEEN_TTL    = timedelta(days=7)
_seen: dict  = {}   # uid → ISO timestamp string


def _load_seen():
    """Load seen entries from disk, pruning anything older than TTL."""
    global _seen
    if not os.path.exists(SEEN_FILE):
        _seen = {}
        return
    try:
        with open(SEEN_FILE, "r") as f:
            raw = json.load(f)
        cutoff = datetime.now(timezone.utc) - _SEEN_TTL
        _seen = {
            uid: ts for uid, ts in raw.items()
            if datetime.fromisoformat(ts) > cutoff
        }
        log.info(f"Loaded {len(_seen)} seen entries from disk")
    except Exception as e:
        log.warning(f"Could not load seen file: {e} — starting fresh")
        _seen = {}


def _save_seen():
    """Write current seen entries to disk."""
    try:
        with open(SEEN_FILE, "w") as f:
            json.dump(_seen, f)
    except Exception as e:
        log.warning(f"Could not save seen file: {e}")


def _make_uid(company: str, title: str, url: str = "") -> str:
    """
    Build a stable deduplication key from company + title.
    URL alone is unreliable — many boards append tracking params or rotate
    URLs for the same posting. Company + title is stable across sources.
    We normalise both fields so minor formatting differences don't create
    false "new" entries (e.g. "VP, Finance" vs "VP Finance").
    """
    def norm(s: str) -> str:
        s = s.lower().strip()
        s = re.sub(r"[^a-z0-9]+", "-", s)   # non-alphanum → dash
        s = re.sub(r"-+", "-", s).strip("-") # collapse dashes
        return s

    company_key = norm(company)
    title_key   = norm(title)
    return f"{company_key}::{title_key}"


def is_new(uid: str = "", company: str = "", title: str = "", url: str = "") -> bool:
    """
    Return True if this job has not been seen before.
    Accepts either a raw uid string OR company+title fields.
    Prefers the compound company+title key when both are provided.
    Marks the entry as seen if it is new.
    """
    key = _make_uid(company, title, url) if (company and title) else uid
    if key in _seen:
        return False
    _seen[key] = datetime.now(timezone.utc).isoformat()
    _save_seen()
    return True


# ─────────────────────────────────────────────────────────────────────────────
# TEXT HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def clean(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", text).strip()


def lower(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


# ─────────────────────────────────────────────────────────────────────────────
# PRE-FILTERS  (cheap gates before AI)
# ─────────────────────────────────────────────────────────────────────────────

def title_matches(title: str) -> bool:
    t = lower(title)
    return any(lower(target) in t for target in TARGET_TITLES)


def has_signal(description: str) -> bool:
    """At least one ERP signal phrase must appear before we call Claude."""
    desc = lower(description)
    return any(lower(phrase) in desc for phrase in ALL_SIGNAL_PHRASES)


def get_matched_phrases(description: str, limit: int = 4) -> list[str]:
    desc  = lower(description)
    found = []
    for phrase in URGENT_SIGNAL_PHRASES + WATCHLIST_SIGNAL_PHRASES:
        if lower(phrase) in desc and phrase not in found:
            found.append(phrase)
        if len(found) >= limit:
            break
    return found


# ─────────────────────────────────────────────────────────────────────────────
# SOURCE 1 — JSEARCH  (LinkedIn + Indeed + Glassdoor + 25 more boards)
# Runs on a schedule (2x daily) to stay within RapidAPI Basic plan limits.
# ─────────────────────────────────────────────────────────────────────────────

def should_run_jsearch() -> bool:
    """
    Return True if we are within 5 minutes of a configured sweep time.
    Sweep times are [hour, minute] pairs in UTC defined in config.py.
    _last_jsearch_sweep prevents double-running within the same window.

    Current schedule (PST = UTC-8):
      7:30 AM PST  →  15:30 UTC
      4:00 PM PST  →  00:00 UTC
    """
    global _last_jsearch_sweep
    now = datetime.now(timezone.utc)

    in_window = any(
        now.hour == h and abs(now.minute - m) < 5
        for h, m in JSEARCH_SWEEP_TIMES_UTC
    )
    if not in_window:
        return False

    # Don't re-run if we already swept in the last 6 hours
    # (prevents double-firing within the same 5-minute window)
    if _last_jsearch_sweep:
        hours_since = (now - _last_jsearch_sweep).total_seconds() / 3600
        if hours_since < 6:
            return False

    return True


def fetch_jsearch() -> list[dict]:
    """
    Call JSearch API for each configured query.
    JSearch aggregates LinkedIn, Indeed, Glassdoor, ZipRecruiter, and 25+ boards.
    Returns a list of normalised job dicts ready for pre-filtering.
    """
    global _last_jsearch_sweep

    api_key = os.environ.get("JSEARCH_API_KEY", "")
    if not api_key:
        log.warning("  JSEARCH_API_KEY not set — skipping JSearch sweep")
        return []

    headers = {
        "X-RapidAPI-Key":  api_key,
        "X-RapidAPI-Host": JSEARCH_HOST,
    }

    jobs = []
    for query in JSEARCH_QUERIES:
        log.info(f"  [JSearch] Searching: '{query}'…")
        try:
            r = requests.get(
                JSEARCH_BASE_URL,
                headers=headers,
                params={
                    "query":            query,
                    "page":             "1",
                    "num_pages":        "1",
                    "date_posted":      JSEARCH_DATE_POSTED,
                    "results_per_page": str(JSEARCH_RESULTS_PER_PAGE),
                },
                timeout=20,
            )
            r.raise_for_status()
            data = r.json()
        except requests.RequestException as e:
            log.warning(f"  JSearch error ('{query}'): {e}")
            continue

        for item in data.get("data", []):
            title   = item.get("job_title", "")
            company = item.get("employer_name", "Unknown")
            city    = item.get("job_city", "")
            state   = item.get("job_state", "")
            country = item.get("job_country", "US")
            loc     = ", ".join(filter(None, [city, state, country]))
            url     = item.get("job_apply_link", "") or item.get("job_google_link", "")
            desc    = clean(item.get("job_description", ""))
            website = item.get("employer_website")   # JSearch returns this directly
            remote  = bool(item.get("job_is_remote", False))
            posted  = item.get("job_posted_at_datetime_utc", "")

            if not is_new(company=company, title=title, url=url):
                continue
            if not title_matches(title):
                continue
            if not has_signal(desc):
                continue

            jobs.append({
                "source":           "JSearch (LinkedIn/Indeed/Glassdoor)",
                "title":            title,
                "company":          company,
                "location":         loc,
                "url":              url,
                "description":      desc[:2000],
                "posted":           posted[:10] if posted else "Recent",
                "is_remote":        remote,
                "employer_website": website,   # pass through to skip AI inference
            })
            log.info(f"  [JSearch] {title} @ {company} ({loc})")

    if jobs or should_run_jsearch():
        _last_jsearch_sweep = datetime.now(timezone.utc)
        log.info(f"  [JSearch] Sweep complete — {len(jobs)} new candidate(s)")

    return jobs


# ─────────────────────────────────────────────────────────────────────────────
# SOURCE 2 — THE MUSE  (finance/accounting category, free, no key)
# ─────────────────────────────────────────────────────────────────────────────

def fetch_muse() -> list[dict]:
    jobs = []
    try:
        r = requests.get(
            "https://www.themuse.com/api/public/jobs",
            params={"category": "Finance and Accounting", "page": 0, "descending": True},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
    except requests.RequestException as e:
        log.warning(f"  The Muse: {e}")
        return []

    for item in data.get("results", []):
        title   = item.get("name", "")
        company = item.get("company", {}).get("name", "Unknown")
        url     = item.get("refs", {}).get("landing_page", "")
        uid     = f"muse-{url or company + title}"
        locations = item.get("locations", [])
        loc     = locations[0].get("name", "") if locations else ""
        desc    = clean(item.get("contents", ""))

        if not is_new(company=company, title=title, url=url): continue
        if not title_matches(title): continue
        if not has_signal(desc): continue

        jobs.append({
            "source": "The Muse", "title": title, "company": company,
            "location": loc, "url": url, "description": desc[:2000],
            "posted": "Recent",
            "is_remote": "remote" in lower(loc + " " + title),
        })
        log.info(f"  [Muse] {title} @ {company}")

    return jobs


# ─────────────────────────────────────────────────────────────────────────────
# SOURCE 3 — REMOTIVE  (remote finance roles, free, no key)
# ─────────────────────────────────────────────────────────────────────────────

def fetch_remotive() -> list[dict]:
    jobs = []
    try:
        r = requests.get(
            "https://remotive.com/api/remote-jobs",
            params={"category": "finance", "limit": 100},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
    except requests.RequestException as e:
        log.warning(f"  Remotive: {e}")
        return []

    for item in data.get("jobs", []):
        title   = item.get("title", "")
        company = item.get("company_name", "Unknown")
        url     = item.get("url", "")
        uid     = f"remotive-{url or company + title}"
        desc    = clean(item.get("description", ""))
        loc     = item.get("candidate_required_location", "Remote")

        if not is_new(company=company, title=title, url=url): continue
        if not title_matches(title): continue
        if not has_signal(desc): continue

        jobs.append({
            "source": "Remotive", "title": title, "company": company,
            "location": loc, "url": url, "description": desc[:2000],
            "posted": "Recent",
            "is_remote": True,  # Remotive is always remote
        })
        log.info(f"  [Remotive] {title} @ {company}")

    return jobs


# ─────────────────────────────────────────────────────────────────────────────
# OPTIONAL SPEED LAYER — DIRECT ATS POLLING
# Only runs if companies have been added to the lists in config.py.
# ─────────────────────────────────────────────────────────────────────────────

def fetch_greenhouse(company: dict) -> list[dict]:
    slug, name = company["slug"], company["name"]
    try:
        r = requests.get(
            f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true",
            timeout=10,
        )
        if r.status_code == 404: return []
        r.raise_for_status()
    except requests.RequestException:
        return []

    jobs = []
    for job in r.json().get("jobs", []):
        title   = job.get("title", "")
        url     = job.get("absolute_url", "")
        uid     = f"gh-{job.get('id', url)}"
        desc    = clean(job.get("content", ""))
        loc_raw = job.get("location", {})
        loc     = loc_raw.get("name", "") if isinstance(loc_raw, dict) else str(loc_raw)

        if not is_new(company=name, title=title, url=url): continue
        if not title_matches(title): continue
        if not has_signal(desc): continue

        jobs.append({
            "source": f"Greenhouse (direct — {name})", "title": title,
            "company": name, "location": loc, "url": url,
            "description": desc[:2000], "posted": "Real-time",
            "is_remote": "remote" in lower(title + " " + loc),
        })
        log.info(f"  [GH-direct] {title} @ {name}")
    return jobs


def fetch_lever(company: dict) -> list[dict]:
    slug, name = company["slug"], company["name"]
    try:
        r = requests.get(
            f"https://api.lever.co/v0/postings/{slug}?mode=json",
            timeout=10,
        )
        if r.status_code == 404: return []
        r.raise_for_status()
    except requests.RequestException:
        return []

    jobs = []
    for p in r.json():
        title   = p.get("text", "")
        url     = p.get("hostedUrl", "")
        uid     = f"lv-{p.get('id', url)}"
        desc    = clean(p.get("descriptionPlain", "") or p.get("description", ""))
        for lst in p.get("lists", []):
            desc += " " + clean(lst.get("content", ""))
        loc = p.get("categories", {}).get("location", "") or p.get("workplaceType", "")

        if not is_new(company=name, title=title, url=url): continue
        if not title_matches(title): continue
        if not has_signal(desc): continue

        jobs.append({
            "source": f"Lever (direct — {name})", "title": title,
            "company": name, "location": loc or "See posting",
            "url": url, "description": desc[:2000], "posted": "Real-time",
            "is_remote": "remote" in lower(title + " " + loc),
        })
        log.info(f"  [LV-direct] {title} @ {name}")
    return jobs


def fetch_ashby(company: dict) -> list[dict]:
    slug, name = company["slug"], company["name"]
    try:
        r = requests.post(
            "https://jobs.ashbyhq.com/api/non-authenticated-open-job-listings",
            json={"organizationHostedJobsPageName": slug},
            timeout=10,
        )
        if r.status_code in (400, 404): return []
        r.raise_for_status()
    except requests.RequestException:
        return []

    jobs = []
    for job in r.json().get("jobPostings", []):
        title   = job.get("title", "")
        job_id  = job.get("id", "")
        url     = f"https://jobs.ashbyhq.com/{slug}/{job_id}"
        uid     = f"ab-{job_id}"
        desc    = clean(job.get("descriptionHtml", "") or job.get("description", ""))
        loc     = job.get("locationName", "")

        if not is_new(company=name, title=title, url=url): continue
        if not title_matches(title): continue
        if not has_signal(desc): continue

        jobs.append({
            "source": f"Ashby (direct — {name})", "title": title,
            "company": name, "location": loc or "See posting",
            "url": url, "description": desc[:2000], "posted": "Real-time",
            "is_remote": "remote" in lower(title + " " + loc),
        })
        log.info(f"  [AB-direct] {title} @ {name}")
    return jobs


# ─────────────────────────────────────────────────────────────────────────────
# AI SCORING — Claude reads the JD and determines ICP fit + territory + score
# ─────────────────────────────────────────────────────────────────────────────

SCORE_PROMPT = f"""\
You are a sales intelligence analyst for a NetSuite ERP account executive.

The rep sells NetSuite to mid-market companies ($5M–$100M annual revenue).

TARGET ICP INDUSTRIES (the company must appear to operate in one of these):
{chr(10).join(f'  • {i}' for i in ICP_INDUSTRIES)}

TERRITORY (where the company HQ must be located):
  US states: {', '.join(TERRITORY_STATES)}
  Canada: {', '.join(TERRITORY_CANADA)}

The rep cares about the COMPANY HQ LOCATION, not where the job is posted.
A remote CFO role posted anywhere is valid if the company HQ is in territory.

Read the job posting below. Return ONLY valid JSON — no markdown, no backticks,
nothing outside the JSON object.

{{
  "icp_industry": <the industry this company most likely operates in, e.g. \
"Food & Beverage — distribution" or "Building Materials — roofing manufacturer". \
Write "Unknown" if you genuinely cannot tell>,
  "icp_match": <true if the industry matches one of the five ICP verticals, \
false if clearly outside (e.g. SaaS, healthcare, finance, government), \
null if you cannot determine>,
  "hq_location": <your best inference of company HQ, e.g. "Portland, OR". \
Use company name, description context, office locations, any clues. null if unknown>,
  "hq_in_territory": <true if HQ appears to be in territory, false if clearly \
outside, null if unknown>,
  "score": <integer 1–10 — NetSuite opportunity strength>,
  "score_reason": <one plain-English sentence explaining the score>,
  "revenue_estimate": <best estimate of annual revenue, e.g. "$20M–$60M". \
Use funding size, employee count, geographic scope, industry benchmarks. \
"Unknown" if no basis>,
  "revenue_confidence": <"low", "medium", or "high">,
  "is_replacement_signal": <true if JD suggests they are replacing or evaluating \
a current system, false otherwise>,
  "company_website": <the company's most likely website URL. First look for it \
explicitly in the job posting. If not stated, infer the most probable URL from \
the company name — e.g. "Pacific Ridge Foods" -> "www.pacificridgefoods.com". \
Always return your best guess as a full URL starting with www. Append "(inferred)" \
if guessing rather than reading it from the posting. Return null only if the \
company name is too generic to make any reasonable guess.>
}}

SCORING GUIDE:
  9–10  JD names a specific ERP or competitor, mentions prior implementation/\
evaluation experience, or signals active system selection. Company is clearly \
in ICP industry and territory.
  7–8   Strong ERP-adjacent signals (multi-entity, consolidation, digital \
transformation) + company is in ICP industry.
  5–6   Company in ICP industry but ERP urgency is unclear from the JD.
  3–4   Weak industry fit or no meaningful ERP signal.
  1–2   Wrong industry, too large, or generic posting with no relevant signals.

JOB POSTING:
"""


def score_job(job: dict) -> dict:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        job.update({
            "ai_score": 5, "ai_reason": "AI unavailable — ANTHROPIC_API_KEY not set",
            "icp_industry": "Unknown", "icp_match": None,
            "hq_location": None, "hq_in_territory": None,
            "revenue_estimate": "Unknown", "revenue_confidence": "low",
            "is_replacement_signal": False,
        })
        return job

    text = (
        f"TITLE: {job['title']}\n"
        f"COMPANY: {job['company']}\n"
        f"POSTED LOCATION: {job['location']}\n"
        f"SOURCE: {job['source']}\n\n"
        f"DESCRIPTION:\n{job['description']}"
    )

    try:
        client   = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=600,
            messages=[{"role": "user", "content": SCORE_PROMPT + text}],
        )
        raw = response.content[0].text.strip()
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
        parsed = json.loads(raw)

        job.update({
            "ai_score":              int(parsed.get("score", 0)),
            "ai_reason":             str(parsed.get("score_reason", "—")),
            "icp_industry":          str(parsed.get("icp_industry", "Unknown")),
            "icp_match":             parsed.get("icp_match"),
            "hq_location":           parsed.get("hq_location"),
            "hq_in_territory":       parsed.get("hq_in_territory"),
            "revenue_estimate":      str(parsed.get("revenue_estimate", "Unknown")),
            "revenue_confidence":    str(parsed.get("revenue_confidence", "low")),
            "is_replacement_signal": bool(parsed.get("is_replacement_signal", False)),
            # Use employer_website from JSearch if provided — skip AI inference
            "company_website": (
                job.get("employer_website")
                or parsed.get("company_website")
            ),
        })
        log.info(
            f"  AI {job['ai_score']}/10  [{job['icp_industry']}]  "
            f"HQ:{job['hq_location']}  {job['ai_reason'][:60]}"
        )

    except json.JSONDecodeError:
        log.warning(f"  AI JSON error for {job['company']}")
        job.update({
            "ai_score": 5, "ai_reason": "AI parse error",
            "icp_industry": "Unknown", "icp_match": None,
            "hq_location": None, "hq_in_territory": None,
            "revenue_estimate": "Unknown", "revenue_confidence": "low",
            "is_replacement_signal": False,
            "company_website": None,
        })
    except Exception as e:
        log.warning(f"  AI error ({job['company']}): {e}")
        job.update({
            "ai_score": 5, "ai_reason": f"AI error: {str(e)[:60]}",
            "icp_industry": "Unknown", "icp_match": None,
            "hq_location": None, "hq_in_territory": None,
            "revenue_estimate": "Unknown", "revenue_confidence": "low",
            "is_replacement_signal": False,
            "company_website": None,
        })

    return job


# ─────────────────────────────────────────────────────────────────────────────
# DISCORD
# ─────────────────────────────────────────────────────────────────────────────

def score_bar(n: int) -> str:
    return f"`{'█' * n}{'░' * (10 - n)}`"


def get_webhook(score: int) -> str:
    if score >= URGENT_THRESHOLD:
        url = os.environ.get("DISCORD_URGENT_WEBHOOK_URL", "")
        if url:
            return url
    return os.environ.get("DISCORD_WATCHLIST_WEBHOOK_URL", "")


def send_alert(job: dict) -> bool:
    score   = job.get("ai_score", 0)
    webhook = get_webhook(score)
    if not webhook:
        log.error("  No Discord webhook configured")
        return False

    channel = "🔴  URGENT" if score >= URGENT_THRESHOLD else "🔵  WATCH LIST"

    hq  = job.get("hq_location")
    in_t = job.get("hq_in_territory")
    if hq:
        if in_t is True:
            hq_line = f"{hq}  ✅  In territory"
        elif in_t is False:
            hq_line = f"{hq}  ⚠️  Outside territory — verify before acting"
        else:
            hq_line = f"{hq}  ❓  Verify territory in ZoomInfo"
    else:
        hq_line = "⚠️  HQ unknown — verify in ZoomInfo before acting"

    rev = job.get("revenue_estimate", "Unknown")
    if rev != "Unknown":
        rev = f"{rev}  ({job.get('revenue_confidence', 'low')} confidence)"

    phrases = get_matched_phrases(job.get("description", ""))
    phrase_text = "\n".join(f"• {p}" for p in phrases) or "See posting"

    icp_line = job.get("icp_industry", "Unknown")
    if job.get("icp_match") is True:
        icp_line += "  ✅"
    elif job.get("icp_match") is False:
        icp_line += "  ⚠️  Outside ICP — review before acting"

    remote_tag = "  •  🌐 Remote" if job.get("is_remote") else ""
    repl_tag   = "✅  System replacement likely" if job.get("is_replacement_signal") else "—"

    website = job.get("company_website")
    website_line = website if website else "Not found — search company name in ZoomInfo"

    fields = [
        {"name": f"Score  {score}/10  •  {channel}", "value": score_bar(score), "inline": False},
        {"name": "Why this matters",    "value": job.get("ai_reason", "—"),              "inline": False},
        {"name": "Industry",            "value": icp_line,                                "inline": False},
        {"name": "Company HQ",          "value": hq_line,                                 "inline": True},
        {"name": "Posted location",     "value": job.get("location","—") + remote_tag,   "inline": True},
        {"name": "Company website",     "value": website_line,                            "inline": False},
        {"name": "Estimated revenue",   "value": rev,                                     "inline": True},
        {"name": "Replacement signal",  "value": repl_tag,                                "inline": True},
        {"name": "ERP signal phrases",  "value": phrase_text,                             "inline": False},
        {"name": "Source",              "value": f"{job.get('source','—')}  •  {job.get('posted','—')}", "inline": False},
        {"name": "Next steps",          "value": "1. Look up website above in ZoomInfo  2. Confirm HQ and revenue  3. Find hiring manager on LinkedIn", "inline": False},
    ]

    embed = {
        "title":     f"💼  {job.get('company','Unknown')}  —  {job.get('title','Finance Role')}",
        "url":       job.get("url", ""),
        "color":     0x378ADD if score >= URGENT_THRESHOLD else 0x888780,
        "fields":    fields,
        "footer":    {"text": "Territory Monitor — Jobs"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    try:
        r = requests.post(
            webhook,
            json={"username": "Territory Monitor — Jobs", "embeds": [embed]},
            timeout=10,
        )
        r.raise_for_status()
        return True
    except requests.RequestException as e:
        log.error(f"  Discord failed: {e}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# HEARTBEAT
# ─────────────────────────────────────────────────────────────────────────────

_last_heartbeat: datetime | None = None


def maybe_heartbeat():
    global _last_heartbeat
    now = datetime.now(timezone.utc)
    if not (now.hour == HEARTBEAT_HOUR_UTC and now.minute < HEARTBEAT_MINUTE_UTC + 6):
        return
    if _last_heartbeat and (now - _last_heartbeat).total_seconds() < 23 * 3600:
        return

    webhook = os.environ.get("DISCORD_WATCHLIST_WEBHOOK_URL", "")
    if not webhook:
        return

    ats_count = len(GREENHOUSE_COMPANIES) + len(LEVER_COMPANIES) + len(ASHBY_COMPANIES)
    ats_note  = (
        f"{ats_count} direct ATS accounts being monitored in real-time"
        if ats_count > 0
        else "No direct ATS accounts yet — add in config.py for real-time speed"
    )

    embed = {
        "title":  "🟢  Job Monitor — Daily Check-in",
        "color":  0x1D9E75,
        "description": (
            "Running normally — polling every 5 minutes.\n"
            "AI determines ICP fit from each job description. "
            "No static company list required."
        ),
        "fields": [
            {"name": "Status",          "value": "✅  Running continuously",                      "inline": False},
            {"name": "Check-in time",   "value": now.strftime("%A %B %d %Y  •  %H:%M UTC"),      "inline": False},
            {"name": "Broad market (2x daily)", "value": "JSearch — LinkedIn  •  Indeed  •  Glassdoor  •  ZipRecruiter  •  25+ boards", "inline": False},
        {"name": "Fast layer (every 5 min)",  "value": "The Muse  •  Remotive  •  Direct ATS", "inline": False},
            {"name": "Direct ATS",      "value": ats_note,                                         "inline": False},
            {"name": "Score routing",   "value": f"**{URGENT_THRESHOLD}–10** → #urgent  |  **{WATCHLIST_MIN}–{URGENT_THRESHOLD-1}** → #watch-list", "inline": False},
            {"name": "ICP filter",      "value": "Claude reads each JD and determines industry + territory fit dynamically", "inline": False},
        ],
        "footer":    {"text": "Territory Monitor — Jobs  •  Daily heartbeat"},
        "timestamp": now.isoformat(),
    }

    try:
        r = requests.post(
            webhook,
            json={"username": "Territory Monitor — Jobs", "embeds": [embed]},
            timeout=10,
        )
        r.raise_for_status()
        _last_heartbeat = now
        log.info("Job monitor heartbeat sent")
    except requests.RequestException as e:
        log.warning(f"Heartbeat failed: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN POLL CYCLE
# ─────────────────────────────────────────────────────────────────────────────

def run_cycle() -> dict:
    candidates = []

    # ── JSearch broad market sweep (2x daily — LinkedIn/Indeed/Glassdoor) ──
    if should_run_jsearch():
        log.info("  [JSearch] Running scheduled broad market sweep…")
        candidates.extend(fetch_jsearch())
    else:
        log.info("  [JSearch] Outside sweep window — skipping this cycle")

    # ── Free sources (every cycle) ──
    candidates.extend(fetch_muse())
    candidates.extend(fetch_remotive())

    # ── Optional speed layer (only runs if companies are configured) ──
    if GREENHOUSE_COMPANIES:
        log.info(f"  [GH-direct] Polling {len(GREENHOUSE_COMPANIES)} companies…")
        for c in GREENHOUSE_COMPANIES:
            candidates.extend(fetch_greenhouse(c))

    if LEVER_COMPANIES:
        log.info(f"  [LV-direct] Polling {len(LEVER_COMPANIES)} companies…")
        for c in LEVER_COMPANIES:
            candidates.extend(fetch_lever(c))

    if ASHBY_COMPANIES:
        log.info(f"  [AB-direct] Polling {len(ASHBY_COMPANIES)} companies…")
        for c in ASHBY_COMPANIES:
            candidates.extend(fetch_ashby(c))

    if not candidates:
        log.info("  No new candidates this cycle")
        return {"candidates": 0, "sent": 0}

    log.info(f"  Pre-filter passed: {len(candidates)} — sending to AI…")
    scored = [score_job(j) for j in candidates]

    # Suppress wrong industry or clearly outside territory
    actionable = [
        j for j in scored
        if j.get("icp_match") is not False
        and j.get("hq_in_territory") is not False
    ]
    dropped = len(scored) - len(actionable)
    if dropped:
        log.info(f"  AI suppressed {dropped} (wrong industry or territory)")

    to_send    = [j for j in actionable if j.get("ai_score", 0) >= WATCHLIST_MIN]
    suppressed = [j for j in actionable if j.get("ai_score", 0) < WATCHLIST_MIN]
    for j in suppressed:
        log.info(f"  Score suppressed [{j['ai_score']}/10]: {j['company']}")

    sent = 0
    for job in sorted(to_send, key=lambda x: x.get("ai_score", 0), reverse=True):
        ch = "#urgent" if job["ai_score"] >= URGENT_THRESHOLD else "#watch-list"
        log.info(f"  Sending [{job['ai_score']}/10] → {ch}: {job['company']} — {job['title']}")
        if send_alert(job):
            sent += 1

    return {"candidates": len(candidates), "sent": sent}


# ─────────────────────────────────────────────────────────────────────────────
# STARTUP VALIDATION
# ─────────────────────────────────────────────────────────────────────────────

def validate_env() -> bool:
    missing = []
    required = {
        "DISCORD_WATCHLIST_WEBHOOK_URL": "your #watch-list Discord webhook",
        "DISCORD_URGENT_WEBHOOK_URL":    "your #urgent Discord webhook",
        "ANTHROPIC_API_KEY":             "your Anthropic API key",
    }

    for var, desc in required.items():
        if not os.environ.get(var):
            missing.append(f"  {var}  ({desc})")

    if missing:
        log.error("Missing required environment variables:")
        for m in missing: log.error(m)
        log.error("Add these in Railway → Variables tab.")
        return False

    if not os.environ.get("JSEARCH_API_KEY"):
        log.warning("JSEARCH_API_KEY not set — JSearch sweeps will be skipped.")
        log.warning("The Muse and Remotive will still run every 5 minutes.")

    return True


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def main():
    log.info("=" * 60)
    log.info("Territory Job Monitor  (v2 — AI-first ICP filtering)")
    log.info(f"Poll: every {POLL_INTERVAL_SECONDS // 60} min  |  "
             f"Routing: {URGENT_THRESHOLD}–10 → #urgent  |  {WATCHLIST_MIN}–{URGENT_THRESHOLD-1} → #watch-list")
    log.info("Broad market: JSearch sweeps LinkedIn/Indeed/Glassdoor 2x daily")
    log.info("Fast layer:   The Muse + Remotive + direct ATS every 5 minutes")
    ats = len(GREENHOUSE_COMPANIES) + len(LEVER_COMPANIES) + len(ASHBY_COMPANIES)
    log.info(f"Direct ATS: {ats} companies configured")
    log.info("=" * 60)

    if not validate_env():
        sys.exit(1)

    _load_seen()

    cycle = 0
    while True:
        cycle += 1
        log.info(f"─── Cycle {cycle} ──────────────────────────────")
        try:
            stats = run_cycle()
            log.info(f"Cycle {cycle}: {stats['candidates']} candidates, {stats['sent']} sent")
        except Exception as e:
            log.error(f"Unhandled error in cycle {cycle}: {e}", exc_info=True)

        try:
            maybe_heartbeat()
        except Exception as e:
            log.warning(f"Heartbeat error: {e}")

        log.info(f"Sleeping {POLL_INTERVAL_SECONDS // 60} min…\n")
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
