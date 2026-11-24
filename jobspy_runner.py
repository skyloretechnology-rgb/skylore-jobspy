"""
Skylore JobSpy runner
---------------------
Runs on GitHub Actions (free). For one Skylore Job Bot sheet it:
  1. asks the sheet which job titles to search (the sheet's web app, action=config),
  2. searches LinkedIn, Indeed, Glassdoor and ZipRecruiter with JobSpy,
  3. drops jobs that are clearly wrong (too old, not full-time, duplicates),
  4. sends the rest back to the sheet's _Inbox tab in small batches.

The sheet's own 5 PM run then filters, scores and assigns these jobs together
with the JSearch (Google Jobs) results.

Needs two environment variables (GitHub secrets):
  SHEET_URL   = the sheet's web app URL (ends with /exec)
  SHEET_TOKEN = the token shown in the sheet menu "3. JobSpy link"
"""
import datetime as dt
import os
import random
import sys
import time

import requests
from jobspy import scrape_jobs

SHEET_URL = os.environ.get("SHEET_URL", "").strip()
SHEET_TOKEN = os.environ.get("SHEET_TOKEN", "").strip()
SHEET_NAME = os.environ.get("SHEET_NAME", "sheet")

BAD_TYPES = ("parttime", "part-time", "contract", "internship", "temporary", "perdiem")
SITE_NAMES = {
    "linkedin": "LinkedIn",
    "indeed": "Indeed",
    "glassdoor": "Glassdoor",
    "zip_recruiter": "ZipRecruiter",
    "google": "Google",
}


def log(msg):
    print(f"[{dt.datetime.utcnow():%H:%M:%S}] {msg}", flush=True)


def call_sheet(payload, method="post"):
    payload = dict(payload, token=SHEET_TOKEN)
    for attempt in range(3):
        try:
            if method == "get":
                r = requests.get(SHEET_URL, params=payload, timeout=120)
            else:
                # Apps Script answers a POST with a redirect to the result; requests follows it.
                r = requests.post(SHEET_URL, json=payload, timeout=300)
            data = r.json()
            if not data.get("ok"):
                raise RuntimeError(data.get("error", "sheet said not ok"))
            return data
        except Exception as e:  # noqa: BLE001
            log(f"sheet call failed ({attempt + 1}/3): {e}")
            time.sleep(10 * (attempt + 1))
    raise RuntimeError("could not reach the sheet")


def clean(v, limit=None):
    if v is None:
        return ""
    try:
        import math
        if isinstance(v, float) and math.isnan(v):
            return ""
    except Exception:  # noqa: BLE001
        pass
    s = str(v)
    if s in ("nan", "NaT", "None"):
        return ""
    return s[:limit] if limit else s


def to_jobs(df, country, title, max_age_days):
    jobs = []
    today = dt.date.today()
    for _, r in df.iterrows():
        jtype = clean(r.get("job_type")).lower()
        if jtype and "fulltime" not in jtype and any(b in jtype for b in BAD_TYPES):
            continue
        posted = r.get("date_posted")
        posted_s = ""
        if posted is not None and clean(posted):
            try:
                pd_ = posted if isinstance(posted, dt.date) else dt.date.fromisoformat(str(posted)[:10])
                if (today - pd_).days > max_age_days:
                    continue
                posted_s = pd_.isoformat()
            except Exception:  # noqa: BLE001
                pass
        site = clean(r.get("site"))
        jid = clean(r.get("id")) or clean(r.get("job_url"))
        if not jid:
            continue
        jobs.append({
            "id": f"{site}:{jid}",
            "site": SITE_NAMES.get(site, site),
            "title": clean(r.get("title"), 300),
            "company": clean(r.get("company"), 200),
            "location": clean(r.get("location"), 200),
            "posted": posted_s,
            "jobType": jtype,
            "remote": bool(r.get("is_remote")) if clean(r.get("is_remote")) else False,
            "url": clean(r.get("job_url_direct")) or clean(r.get("job_url")),
            "boardUrl": clean(r.get("job_url")),
            "desc": clean(r.get("description"), 8000),
            "query": title,
            "country": country,
            "companyUrl": clean(r.get("company_url_direct")) or clean(r.get("company_url")),
        })
    return jobs


def main():
    if not SHEET_URL or not SHEET_TOKEN:
        log("SHEET_URL or SHEET_TOKEN is missing; add them as GitHub secrets.")
        sys.exit(1)
    started = time.time()
    cfg = call_sheet({"action": "config"}, method="get")
    hours = int(cfg.get("hours", 72))
    max_age_days = max(1, (hours + 23) // 24)
    sites = cfg.get("sites") or ["linkedin", "indeed", "glassdoor", "zip_recruiter"]
    per_site = int(cfg.get("perSite", 25))
    max_minutes = float(cfg.get("maxMinutes", 50))
    gta = cfg.get("gtaSearch", "Toronto, ON")
    searches = []
    for country, titles in (cfg.get("titlesByCountry") or {}).items():
        for t in titles:
            if country == "CA":
                searches.append((country, t, "Canada", True))
                searches.append((country, t, gta, False))
            else:
                searches.append((country, t, "United States", False))
    log(f"{SHEET_NAME}: {len(searches)} searches on {sites}, last {hours}h, {per_site} per site")

    seen, seen_query, batch, sent = set(), set(), [], 0
    counts, errors = {}, {}
    linkedin_blocked = 0
    active_sites = list(sites)

    def flush(force=False):
        nonlocal batch, sent
        while batch and (force or len(batch) >= 150):
            chunk, batch = batch[:150], batch[150:]
            call_sheet({"action": "jobs", "jobs": chunk})
            sent += len(chunk)

    for i, (country, title, location, remote) in enumerate(searches):
        if (time.time() - started) / 60 > max_minutes:
            log("time limit reached, stopping searches")
            break
        if not active_sites:
            break
        try:
            df = scrape_jobs(
                site_name=active_sites,
                search_term=title,
                location=location,
                is_remote=remote,
                results_wanted=per_site,
                hours_old=hours,
                country_indeed="canada" if country == "CA" else "usa",
                fetch_description=True,
                verbose=0,
            )
        except Exception as e:  # noqa: BLE001
            log(f"search failed '{title}' @ {location}: {e}")
            errors["search"] = errors.get("search", 0) + 1
            time.sleep(5)
            continue
        for site, err in (df.attrs.get("errors") or {}).items():
            errors[site] = errors.get(site, 0) + 1
            if site == "linkedin" and ("429" in str(err) or "block" in str(err).lower()):
                linkedin_blocked += 1
                if linkedin_blocked >= 3 and "linkedin" in active_sites:
                    log("LinkedIn keeps blocking; skipping it for the rest of this run")
                    active_sites.remove("linkedin")
        new = 0
        for job in to_jobs(df, country, title, max_age_days):
            key = job["id"]
            if (key, title) in seen_query:
                continue
            seen_query.add((key, title))
            if key in seen:
                # same job found by another title: send a light copy so the sheet
                # knows this title matched too (the description was sent already)
                batch.append(dict(job, desc=""))
                continue
            seen.add(key)
            counts[job["site"]] = counts.get(job["site"], 0) + 1
            batch.append(job)
            new += 1
        log(f"{i + 1}/{len(searches)} '{title}' @ {location}: {len(df)} found, {new} new")
        flush()
        time.sleep(random.uniform(2, 5))

    flush(force=True)
    minutes = round((time.time() - started) / 60, 1)
    call_sheet({"action": "done", "counts": counts, "errors": errors, "sent": sent, "minutes": minutes})
    log(f"done: sent {sent} jobs in {minutes} min; by site {counts}; errors {errors}")


if __name__ == "__main__":
    main()
