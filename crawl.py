"""Public LinkedIn guest job listings for Saudi Arabia: list pages, then each
posting's page, cached so a posting is fetched once.

No login, no cookies, one request every 3 s, back off on 429/999, stop on a
repeated 403. The search list comes from the CRAWL_QUERIES secret as
[[keywords, location, tpr, max], ...]. The log prints counts only.

    python crawl.py --out out.json [--seconds 1500] [--details 600]
"""
import gzip
import html
import json
import os
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote

from curl_cffi import requests as cr

SEARCH = ("https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
          "?keywords={q}&location={loc}&f_TPR={tpr}&start={start}")
POSTING = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{bid}"
INTERVAL = 3.0
DESC_CHARS = 5000
MAX_AGE_DAYS = 30
CACHE = Path(".cache/details.json.gz")
CACHE_DAYS = 45

_CARD = re.compile(r'data-entity-urn="urn:li:jobPosting:(\d+)"(.*?)(?=data-entity-urn="urn:li:jobPosting:|\Z)', re.S)
_TITLE = re.compile(r'base-search-card__title">(.*?)</h3>', re.S)
_COMPANY = re.compile(r'base-search-card__subtitle">(.*?)</h4>', re.S)
_LOCATION = re.compile(r'job-search-card__location">(.*?)</span>', re.S)
_DATE = re.compile(r'<time[^>]*datetime="([^"]+)"')
_DESC = re.compile(r'show-more-less-html__markup[^>]*>(.*?)</div>', re.S)
_CRITERIA = re.compile(r'description__job-criteria-subheader">\s*(.*?)\s*</h3>\s*<span[^>]*>\s*(.*?)\s*</span>', re.S)
_ORG = re.compile(r'topcard__org-name-link[^>]*>\s*(.*?)\s*</a>', re.S)
_CLOSED = re.compile(r"No longer accepting applications", re.I)
# How the job takes applications. Offsite postings carry the employer's link in
# <code id="applyUrl">; LinkedIn's own form (Easy Apply) has none. Each marker is
# counted in stats so a change in LinkedIn's markup shows in the numbers.
_APPLY = {"apply_url": re.compile(r'id="applyUrl"'),
          "offsite": re.compile(r"apply-link-offsite"),
          "onsite": re.compile(r"apply-link-(?:onsite|simple)"),
          "easy_text": re.compile(r"Easy Apply", re.I)}


class Blocked(Exception):
    pass


def _text(fragment, keep_lines=False):
    s = re.sub(r"<(br|/p|/li|/div|/h\d)[^>]*>", "\n", str(fragment or ""), flags=re.I) if keep_lines else str(fragment or "")
    s = html.unescape(re.sub(r"<[^>]+>", " ", s))
    if keep_lines:
        s = "\n".join(re.sub(r"[ \t\xa0]+", " ", ln).strip() for ln in s.splitlines())
        return re.sub(r"\n{3,}", "\n\n", s).strip()
    return re.sub(r"\s+", " ", s).strip()


def _day(value):
    try:
        return datetime.fromisoformat(str(value)[:10]).date().isoformat()
    except (TypeError, ValueError):
        return None


def _too_old(posted_at):
    try:
        return (date.today() - date.fromisoformat(str(posted_at)[:10])).days > MAX_AGE_DAYS
    except (TypeError, ValueError):
        return False


_last = [0.0]


def _get(url, tries=4):
    last = None
    for attempt in range(tries):
        wait = _last[0] + INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.monotonic()
        try:
            r = cr.get(url, impersonate="chrome", timeout=30,
                       headers={"Accept-Language": "en-US,en;q=0.9"}, allow_redirects=True)
        except Exception as e:                      # noqa: BLE001 - network blips retry
            last = type(e).__name__
            time.sleep(5 * (attempt + 1))
            continue
        if r.status_code in (429, 999) or (r.status_code == 403 and attempt == 0):
            last = f"HTTP {r.status_code}"
            time.sleep(min(240, 30 * 2 ** attempt))
            continue
        if r.status_code == 403:
            raise Blocked("HTTP 403 twice")
        return r
    raise Blocked(f"{last} after {tries} tries")


def cards(page):
    rows = []
    for bid, body in _CARD.findall(page):
        t = _TITLE.search(body)
        if not t:
            continue
        co, loc, dt = _COMPANY.search(body), _LOCATION.search(body), _DATE.search(body)
        rows.append({"board": "linkedin", "bid": bid, "title": _text(t.group(1)),
                     "url": f"https://www.linkedin.com/jobs/view/{bid}/",
                     "company": _text(co.group(1)) if co else "",
                     "location": _text(loc.group(1)) if loc else "Saudi Arabia",
                     "posted_at": _day(dt.group(1)) if dt else None, "snippet": "",
                     "easy_apply": bool(_APPLY["easy_text"].search(body))})
    return rows


def listing(queries, deadline, stat):
    seen, out, done = set(), [], 0
    for kw, loc, tpr, cap in queries:
        for start in range(0, int(cap), 10):
            if time.time() >= deadline:
                return out
            r = _get(SEARCH.format(q=quote(kw), loc=quote(loc), tpr=tpr, start=start))
            if r.status_code in (400, 404):
                break
            if r.status_code != 200:
                raise Blocked(f"HTTP {r.status_code}")
            stat["pages"] += 1
            rows = cards(r.text)
            if not rows:
                break
            out += [x for x in rows if x["bid"] not in seen]
            seen.update(x["bid"] for x in rows)
            if len(rows) < 10:
                break
        done += 1
    stat["complete"] = done == len(queries)
    return out


def detail(bid):
    r = _get(POSTING.format(bid=bid))
    if r.status_code in (404, 410):
        return {"closed": True}
    if r.status_code != 200:
        return None
    t = r.text
    if _CLOSED.search(t):
        return {"closed": True}
    crit = {k.strip().lower(): _text(v) for k, v in _CRITERIA.findall(t)}
    d, org = _DESC.search(t), _ORG.search(t)
    level = crit.get("seniority level") or ""
    marks = [k for k, rx in _APPLY.items() if rx.search(t)]
    apply = ("site" if {"apply_url", "offsite"} & set(marks) else
             "easy" if {"onsite", "easy_text"} & set(marks) else None)
    return {"description": _text(d.group(1), keep_lines=True)[:DESC_CHARS] if d else "",
            "company": _text(org.group(1)) if org else None,
            "employment_type": crit.get("employment type"),
            "career_level": level if re.search(r"director|executive|internship", level, re.I) else None,
            "apply": apply, "apply_marks": marks}


def _load_cache():
    try:
        with gzip.open(CACHE, "rt", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_cache(cache):
    cut = time.time() - CACHE_DAYS * 86400
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(CACHE, "wt", encoding="utf-8") as f:
        json.dump({k: v for k, v in cache.items() if v.get("t", 0) >= cut}, f, ensure_ascii=False)


def main(out, seconds, budget):
    queries = json.loads(os.environ["CRAWL_QUERIES"])
    stat = {"pages": 0, "listed": 0, "old": 0, "details": 0, "cached": 0, "complete": False, "error": None}
    t0 = time.time()
    rows = []
    try:
        rows = listing(queries, t0 + seconds, stat)
    except Blocked as e:
        stat["error"] = f"listing: {e}"
    stat["listed"] = len(rows)
    stat["card_easy"] = sum(1 for r in rows if r.get("easy_apply"))
    print(f"listed {len(rows)} over {stat['pages']} page(s){'' if stat['complete'] else ' (incomplete)'}")

    cache = _load_cache()
    fresh = [r for r in rows if not _too_old(r["posted_at"])]
    stat["old"] = len(rows) - len(fresh)
    fresh.sort(key=lambda r: r.get("posted_at") or "", reverse=True)
    details = {}
    for r in fresh:
        hit = cache.get(r["bid"])
        if hit and ("apply" in hit["d"] or hit["d"].get("closed")):   # older entries lack apply
            details[r["bid"]] = hit["d"]
            stat["cached"] += 1
            continue
        if budget <= 0 or (stat["error"] or "").startswith("details"):
            continue
        try:
            d = detail(r["bid"])
        except Blocked as e:
            stat["error"] = f"details: {e}"
            continue
        budget -= 1
        if d is not None:
            cache[r["bid"]] = {"t": int(time.time()), "d": d}
            details[r["bid"]] = d
            stat["details"] += 1
            for k in d.get("apply_marks") or []:
                stat["m_" + k] = stat.get("m_" + k, 0) + 1
            stat["apply_" + str(d.get("apply"))] = stat.get("apply_" + str(d.get("apply")), 0) + 1
    _save_cache(cache)
    stat["at"] = int(time.time())
    Path(out).write_text(json.dumps({"at": stat["at"], "stats": stat, "rows": fresh, "details": details},
                                    ensure_ascii=False), encoding="utf-8")
    print(f"details {stat['details']} fetched, {stat['cached']} cached, {stat['old']} too old"
          f"{' - ' + stat['error'] if stat['error'] else ''} ({(time.time() - t0) / 60:.1f} min)")
    return 0 if stat["listed"] else 1


def _arg(name, default):
    return type(default)(sys.argv[sys.argv.index(name) + 1]) if name in sys.argv else default


if __name__ == "__main__":
    sys.exit(main(_arg("--out", "out.json"), _arg("--seconds", 1500), _arg("--details", 600)))
