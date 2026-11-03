#!/usr/bin/env python3
"""
Maryland 2026 General Election — Live Scraper  (Governor + Question 3)
=====================================================================
Fetches Maryland State Board of Elections (SBOE) results pages for TWO races
and AP's results page (est. % of expected vote counted + total votes per county),
then serves everything as JSON so the tracker page can auto-update.

Built from the MD-6 primary scraper — same endpoints, same drop-feed logic,
now one block of data per race.

ENDPOINTS
  GET  /            → the tracker page (MD_General_Tracker.html, if it sits next to this file)
  GET  /results     → live data  { races: { gov: {...}, q3: {...} }, ... }
  GET  /state       → shared tracker state (vote feeds live here)
  GET  /verify      → operator password check (header X-Operator-Password)
  GET  /health      → {"ok":true}
  GET  /debug       → what the scraper currently sees (URLs, AP race ids, method data)
  POST /state       → operator pushes tracker state          (password)
  POST /config      → tracker config (candidates, priors…)  (no password, same as MD-6)
  POST /urls        → change SBOE / AP URLs without redeploying (password)
  POST /patch-feed  → fix a feed card's method label          (password)

RUN LOCALLY:  python3 MD_General_Scraper.py   (then open http://localhost:8765)
"""

import urllib.request, urllib.error, json, time, threading, re, os, datetime, concurrent.futures

try:
    from zoneinfo import ZoneInfo
    _EASTERN = ZoneInfo("America/New_York")
except Exception:
    _EASTERN = None


def _now_eastern(fmt):
    if _EASTERN:
        return datetime.datetime.now(_EASTERN).strftime(fmt)
    return (datetime.datetime.utcnow() - datetime.timedelta(hours=5)).strftime(fmt)  # EST fallback


# ══════════════════════════════════════════════════════════════════════════════
#  URLS — the defaults below are best guesses from SBOE's 2022/2024 naming.
#  If a page 404s on election night, change it from the tracker
#  (Setup → Live Data → "Send URLs to scraper") — no redeploy needed.
# ══════════════════════════════════════════════════════════════════════════════

# SBOE has used different capitalisation each cycle; the scraper tries each until one works.
SBOE_BASES = [b.strip() for b in os.environ.get("SBOE_BASES", ",".join([
    "https://elections.maryland.gov/elections/2026/general_results/",
    "https://elections.maryland.gov/elections/2026/General_Results/",
    "https://elections.maryland.gov/elections/2026/general_Results/",
    "https://elections.maryland.gov/elections/2026/General_results/",
])).split(",") if b.strip()]

# AP results pages. The scraper opens these in a headless browser and listens for
# AP's data files. PASTE THE REAL LINKS from apnews.com into the tracker on election night.
AP_URL_GOV = os.environ.get("AP_URL_GOV", "https://apnews.com/projects/elections-2026/maryland-governor-results/")
AP_URL_Q3  = os.environ.get("AP_URL_Q3",  "https://apnews.com/projects/elections-2026/maryland-question-3-results/")
AP_EVENT_DATE = os.environ.get("AP_EVENT_DATE", "20261103")   # ignore older races the AP page also loads (e.g. 2024)

SBOE_REFRESH_SECS          = int(os.environ.get("SBOE_REFRESH_SECS", 5))    # statewide + county-breakdown pages
COUNTY_METHOD_REFRESH_SECS = int(os.environ.get("COUNTY_METHOD_REFRESH_SECS", 15))  # 24 by-county pages × 2 races
AP_REFRESH_SECS            = int(os.environ.get("AP_REFRESH_SECS", 15))
PORT                       = int(os.environ.get("PORT", 8765))
OPERATOR_PASSWORD          = os.environ.get("OPERATOR_PASSWORD", "mdnight")

# SBOE numbers jurisdictions alphabetically: Allegany=1 … Worcester=24
COUNTIES = {
    1: "Allegany", 2: "Anne Arundel", 3: "Baltimore City", 4: "Baltimore County", 5: "Calvert",
    6: "Caroline", 7: "Carroll", 8: "Cecil", 9: "Charles", 10: "Dorchester", 11: "Frederick",
    12: "Garrett", 13: "Harford", 14: "Howard", 15: "Kent", 16: "Montgomery",
    17: "Prince George's", 18: "Queen Anne's", 19: "Saint Mary's", 20: "Somerset", 21: "Talbot",
    22: "Washington", 23: "Wicomico", 24: "Worcester",
}
# AP keys counties by FIPS code
FIPS_TO_COUNTY = {
    "24001": "Allegany", "24003": "Anne Arundel", "24005": "Baltimore County", "24009": "Calvert",
    "24011": "Caroline", "24013": "Carroll", "24015": "Cecil", "24017": "Charles", "24019": "Dorchester",
    "24021": "Frederick", "24023": "Garrett", "24025": "Harford", "24027": "Howard", "24029": "Kent",
    "24031": "Montgomery", "24033": "Prince George's", "24035": "Queen Anne's", "24037": "Saint Mary's",
    "24039": "Somerset", "24041": "Talbot", "24043": "Washington", "24045": "Wicomico",
    "24047": "Worcester", "24510": "Baltimore City",
}

RACES = {
    "gov": {
        "label": "Governor",
        "kind": "candidate",
        "method_file":    "gen_results_2026_1.html",
        "county_file":    "gen_detail_results_2026_1_1.html",
        "by_county_file": "gen_results_2026_by_county_{n}.html",
        "anchor_a": "Wes Moore",
        "anchor_b": "Dan Cox",
    },
    "q3": {
        "label": "Question 3",
        "kind": "question",
        "question_no": 3,
        "method_file":    "gen_qresults_2026_3.html",
        "county_file":    "gen_detail_qresults_2026_3_1.html",
        "by_county_file": "gen_qresults_2026_by_county_{n}.html",
        "anchor_a": "For",
        "anchor_b": "Against",
    },
}
RACE_KEYS = list(RACES.keys())
METHOD_KEYS = ("mail", "early", "ed", "provisional")
METHOD_LABELS = {"mail": "Mail-In", "early": "Early Vote", "ed": "Election Day", "provisional": "Provisional"}

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_OK = True
except ImportError:
    PLAYWRIGHT_OK = False

# ── Persistence (Railway volume at /app/data; local folder otherwise) ─────────
_DATA_DIR   = "/app/data" if os.path.isdir("/app/data") else "."
STATE_FILE  = os.path.join(_DATA_DIR, "mdgen_state.json")
CONFIG_FILE = os.path.join(_DATA_DIR, "mdgen_config.json")
URLS_FILE   = os.path.join(_DATA_DIR, "mdgen_urls.json")
HERE        = os.path.dirname(os.path.abspath(__file__))
TRACKER_HTML = os.path.join(HERE, "MD_General_Tracker.html")

_lock = threading.RLock()


def _read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path, obj):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(obj, f)
        os.replace(tmp, path)
    except Exception as e:
        print(f"[Persist] Failed to write {path}: {e}")


stored_state  = _read_json(STATE_FILE, {}) or {}
stored_config = _read_json(CONFIG_FILE, {}) or {}
# URL overrides set from the tracker: { base, gov:{method,county,ap}, q3:{method,county,ap} }
url_overrides = _read_json(URLS_FILE, {}) or {}

_working_base = url_overrides.get("base") or None


def race_url(race, which):
    """Resolve the URL for a race page. Overrides win; otherwise base + default file name."""
    ov = (url_overrides.get(race) or {}).get(which)
    if ov:
        return ov
    base = _working_base or SBOE_BASES[0]
    return base + RACES[race][f"{which}_file"]


def by_county_url(race, n):
    ov = (url_overrides.get(race) or {}).get("by_county")  # pattern containing {n}
    if ov and "{n}" in ov:
        return ov.replace("{n}", str(n))
    base = _working_base or SBOE_BASES[0]
    return base + RACES[race]["by_county_file"].replace("{n}", str(n))


def ap_url(race):
    ov = (url_overrides.get(race) or {}).get("ap")
    if ov is not None:
        return ov
    return AP_URL_GOV if race == "gov" else AP_URL_Q3


def _empty_race():
    return {"method": {}, "county": {}, "countyMethod": {}, "precincts": {},
            "apEevp": {}, "apTotalVotes": {}, "apStatewide": {}, "apExpected": {}, "apTs": None, "ts": None}


latest = {
    "races": {k: _empty_race() for k in RACE_KEYS},
    "raceLabels": {k: v["label"] for k, v in RACES.items()},
    "ts": None,
    "apTs": None,
    "log": [],
    "refreshSecs": SBOE_REFRESH_SECS,
    "config": stored_config,
    "sboeBase": _working_base,
}
_ap_debug = {"races_seen": {}, "last_error": None, "urls": {}}


# ══════════════════════════════════════════════════════════════════════════════
#  HTML HELPERS
# ══════════════════════════════════════════════════════════════════════════════

class NotFound(Exception):
    pass


def fetch_url(url, timeout=20):
    bust = f"{'&' if '?' in url else '?'}_={int(time.time())}"
    req = urllib.request.Request(url + bust, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
        "Cache-Control": "no-cache, no-store",
        "Pragma": "no-cache",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            html = r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise NotFound(url)
        raise
    # SBOE serves a 200 "Page Not Found" page in some setups
    if re.search(r"<h1[^>]*>\s*Page Not Found", html, re.I):
        raise NotFound(url)
    return html


def strip_tags(s):
    s = re.sub(r"<br\s*/?>", " ", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    return s.replace("&amp;", "&").replace("&nbsp;", " ").replace("&#39;", "'").replace("&quot;", '"')


def clean(s):
    return re.sub(r"\s+", " ", strip_tags(s)).strip()


def parse_tables(html):
    """Return list of (start_pos, end_pos, rows). Each row is a list of cleaned cell strings."""
    out = []
    for tm in re.finditer(r"<table[^>]*>(.*?)</table>", html, re.I | re.S):
        rows = []
        for rm in re.finditer(r"<tr[^>]*>(.*?)</tr>", tm.group(1), re.I | re.S):
            cells = [clean(cm.group(1)) for cm in re.finditer(r"<t[hd][^>]*>(.*?)</t[hd]>", rm.group(1), re.I | re.S)]
            if cells:
                rows.append(cells)
        if rows:
            out.append((tm.start(), tm.end(), rows))
    return out


def pv(s):
    """'22,985 (40.38%)' → 22985 ; 'NR' / '' → 0"""
    s = str(s).strip()
    if not s or s.upper() in ("NR", "N/A", "—", "-"):
        return 0
    return int(re.sub(r"[^0-9]", "", s.split("(")[0]) or "0")


_PARTY_RE = re.compile(
    r"winner selected|democratic|republican|libertarian|green|working class party|working class|"
    r"unaffiliated|other candidates|\(write[ -]?in\)|write[ -]?ins?|vote for \d+", re.I)


def norm_name(raw, kind):
    """Normalise a candidate / choice label so both pages agree on the same key."""
    s = re.sub(r"\s+", " ", _PARTY_RE.sub(" ", clean(raw))).strip(" ,")
    if not s or re.sub(r"\W", "", s).lower() in ("totals", "total"):
        return None
    if kind == "question":
        if re.match(r"^for\b", s, re.I):
            return "For"
        if re.match(r"^against\b", s, re.I):
            return "Against"
        return None
    # "Wes Moore and Aruna Miller" → "Wes Moore"
    return re.split(r"\s+and\s+", s, flags=re.I)[0].strip()


def _method_cols(headers):
    try:
        early_i = next(i for i, h in enumerate(headers) if "early" in h.lower())
        ed_i    = next(i for i, h in enumerate(headers) if "election day" in h.lower())
        mail_i  = next(i for i, h in enumerate(headers) if "mail" in h.lower())
    except StopIteration:
        return None
    prov_i = next((i for i, h in enumerate(headers) if "prov" in h.lower()), None)
    return early_i, ed_i, mail_i, prov_i


def _row_methods(row, cols):
    early_i, ed_i, mail_i, prov_i = cols
    g = lambda i: pv(row[i]) if i is not None and i < len(row) else 0
    return {"early": g(early_i), "ed": g(ed_i), "mail": g(mail_i), "provisional": g(prov_i)}


def _precinct_match(text):
    for pat in (r"\(?\s*(\d[\d,]*)\s+of\s+(\d[\d,]*)\s+election\s+day\s+precinct",
                r"(\d[\d,]*)\s+of\s+(\d[\d,]*)\s+precinct"):
        m = re.search(pat, text, re.I)
        if m:
            return {"reporting": int(m.group(1).replace(",", "")), "total": int(m.group(2).replace(",", ""))}
    return None


def _is_question_header(cell, qno):
    return re.sub(r"\s+", " ", cell).strip().lower() == f"question {qno}"


def find_race_table(html, race):
    """Locate the method-column table for a race on a statewide or by-county page.
    Returns (rows, text_before_table) or (None, None)."""
    cfg = RACES[race]
    tables = parse_tables(html)
    low = html.lower()
    if cfg["kind"] == "question":
        prev_end = 0
        for start, end, rows in tables:
            if rows and rows[0] and _is_question_header(rows[0][0], cfg["question_no"]) and _method_cols(rows[0]):
                return rows, html[max(prev_end, start - 4000):start]
            prev_end = end
        # fallback: heading "Question 3" followed by first method table
        m = re.search(r">\s*Question\s+%d\s*<" % cfg["question_no"], html, re.I)
        if m:
            for start, end, rows in tables:
                if start > m.end() and rows and _method_cols(rows[0]):
                    return rows, html[m.start():start]
        return None, None
    # candidate race (Governor): first method table after the "Governor / Lt. Governor" heading
    pos = low.find("governor / lt. governor")
    if pos == -1:
        pos = low.find("governor")
    if pos == -1:
        return None, None
    stop = low.find("comptroller", pos)
    for start, end, rows in tables:
        if start < pos:
            continue
        if stop != -1 and start > stop:
            break
        if rows and _method_cols(rows[0]):
            return rows, html[pos:start]
    return None, None


# ══════════════════════════════════════════════════════════════════════════════
#  SBOE SCRAPES
# ══════════════════════════════════════════════════════════════════════════════

def discover_base(log):
    """Find which SBOE folder spelling is live (only if no explicit override)."""
    global _working_base
    if url_overrides.get("base"):
        _working_base = url_overrides["base"]
        return
    if _working_base:
        return
    for base in SBOE_BASES:
        try:
            fetch_url(base + RACES["gov"]["method_file"], timeout=10)
            _working_base = base
            latest["sboeBase"] = base
            log.append(f"✅ SBOE results folder found: {base}")
            print(f"[SBOE] Using results folder {base}")
            return
        except NotFound:
            continue
        except Exception as e:
            log.append(f"⚠️ SBOE probe {base}: {e}")
            return
    log.append("⏳ SBOE results pages not posted yet (all folder spellings 404)")


def scrape_method_page(race, log):
    cfg = RACES[race]
    url = race_url(race, "method")
    html = fetch_url(url)
    rows, before = find_race_table(html, race)
    if not rows:
        log.append(f"⚠️ [{cfg['label']}] method page: race table not found")
        return
    cols = _method_cols(rows[0])
    method = {}
    for row in rows[1:]:
        name = norm_name(row[0], cfg["kind"])
        if not name:
            continue
        m = _row_methods(row, cols)
        if name in method:   # merge duplicates (e.g. several write-in rows)
            for k in METHOD_KEYS:
                method[name][k] += m[k]
        else:
            method[name] = m
    out = latest["races"][race]
    out["method"] = method
    pm = _precinct_match(before or "") or _precinct_match(html)
    if pm:
        out["precincts"]["statewide"] = pm
    tot = {k: sum(v[k] for v in method.values()) for k in METHOD_KEYS}
    log.append(f"✅ [{cfg['label']}] method: {len(method)} rows — Early {tot['early']:,} | ED {tot['ed']:,} | "
               f"Mail {tot['mail']:,} | Prov {tot['provisional']:,}")


def scrape_county_page(race, log):
    cfg = RACES[race]
    html = fetch_url(race_url(race, "county"))
    county = {}
    for start, end, rows in parse_tables(html):
        if not rows or not rows[0] or "jurisdiction" not in rows[0][0].lower():
            continue
        names = [norm_name(h, cfg["kind"]) for h in rows[0][1:]]
        for row in rows[1:]:
            if not row:
                continue
            juris = re.sub(r"\(.*\)", "", row[0]).strip()
            if not juris or "total" in juris.lower():
                continue
            d = county.setdefault(juris, {})
            for i, nm in enumerate(names, 1):
                if nm and i < len(row):
                    d[nm] = d.get(nm, 0) + pv(row[i])
        break  # ONLY the first jurisdiction table — the second one is "Other Write-Ins"
    out = latest["races"][race]
    if county:
        out["county"] = county
        log.append(f"✅ [{cfg['label']}] county breakdown: {len(county)} jurisdictions")
    else:
        log.append(f"⚠️ [{cfg['label']}] county breakdown: no jurisdiction table")
    pm = _precinct_match(html)
    if pm:
        out["precincts"]["statewide"] = pm


def _scrape_one_county(race, n, cname):
    cfg = RACES[race]
    html = fetch_url(by_county_url(race, n))
    rows, before = find_race_table(html, race)
    if not rows:
        return cname, None, None
    cols = _method_cols(rows[0])
    candidates, totals = {}, None
    for row in rows[1:]:
        if not row:
            continue
        if re.sub(r"\W", "", row[0]).lower() in ("totals", "total"):
            totals = _row_methods(row, cols)
            continue
        nm = norm_name(row[0], cfg["kind"])
        if nm:
            m = _row_methods(row, cols)
            if nm in candidates:
                for k in METHOD_KEYS:
                    candidates[nm][k] += m[k]
            else:
                candidates[nm] = m
    if totals is None:
        totals = {k: sum(c[k] for c in candidates.values()) for k in METHOD_KEYS}
    pm = _precinct_match(before or "")
    return cname, {**totals, "candidates": candidates}, pm


def scrape_county_methods(race, log):
    cfg = RACES[race]
    cm, prec, errs = {}, {}, 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(_scrape_one_county, race, n, c) for n, c in COUNTIES.items()]
        for f in concurrent.futures.as_completed(futs):
            try:
                cname, data, pm = f.result()
                if data:
                    cm[cname] = data
                if pm:
                    prec[cname] = pm
            except Exception:
                errs += 1
    out = latest["races"][race]
    if cm:
        out["countyMethod"] = cm
    for k, v in prec.items():
        out["precincts"][k] = v
    log.append(f"{'✅' if cm else '⚠️'} [{cfg['label']}] by-county method pages: {len(cm)}/24 parsed"
               + (f", {errs} errors" if errs else ""))


# ══════════════════════════════════════════════════════════════════════════════
#  VOTE DROP DETECTION (per race — same logic as MD-6)
# ══════════════════════════════════════════════════════════════════════════════
MAX_RETROACTIVE_CYCLES = 120   # ~30 min at 15s county-method cycles

_prev_county        = {k: {} for k in RACE_KEYS}
_prev_county_method = {k: {} for k in RACE_KEYS}
_unresolved         = {k: {} for k in RACE_KEYS}
for _k in RACE_KEYS:
    _d = (stored_state.get("_drop") or {}).get(_k) or {}
    _prev_county_method[_k] = _d.get("prevCountyMethod", {}) or {}
    _unresolved[_k]         = _d.get("unresolved", {}) or {}


def _best_method(new_m, old_m):
    if not new_m:
        return None
    best = max(METHOD_KEYS, key=lambda m: new_m.get(m, 0) - (old_m or {}).get(m, 0))
    return best if new_m.get(best, 0) - (old_m or {}).get(best, 0) > 0 else None


def detect_drops(race):
    global stored_state
    cfg = RACES[race]
    data = latest["races"][race]
    new_county  = data.get("county", {})
    new_methods = {k: {m: v.get(m, 0) for m in METHOD_KEYS} for k, v in data.get("countyMethod", {}).items()}
    if not new_county:
        return
    if not _prev_county[race]:
        _prev_county[race] = {k: dict(v) for k, v in new_county.items()}
        for k, v in new_methods.items():
            _prev_county_method[race].setdefault(k, dict(v))
        return

    ts = _now_eastern("%-I:%M %p ET")
    feeds = stored_state.setdefault("voteFeeds", {})
    feed = list(feeds.get(race, []))

    # Retroactively label drops whose method wasn't known when they appeared
    done = []
    for county, unres in list(_unresolved[race].items()):
        unres["cycles"] = unres.get("cycles", 0) + 1
        new_m = new_methods.get(county, {})
        mk = _best_method(new_m, unres.get("snap", {}))
        if mk or unres["cycles"] >= MAX_RETROACTIVE_CYCLES:
            if mk:
                for e in feed:
                    if e.get("county") == county and e.get("ts") == unres.get("ts") and e.get("methodKey") is None:
                        e["methodKey"], e["methodLabel"] = mk, METHOD_LABELS[mk]
                        break
            if new_m:
                _prev_county_method[race][county] = dict(new_m)
            done.append(county)
    for c in done:
        del _unresolved[race][c]

    for county, cands in new_county.items():
        if county in _unresolved[race]:
            continue
        old = _prev_county[race].get(county, {})
        delta = sum(cands.values()) - sum(old.values())
        if delta <= 0:
            continue
        cand_deltas = {c: cands[c] - old.get(c, 0) for c in cands if cands[c] - old.get(c, 0) > 0}
        a = cand_deltas.get(cfg["anchor_a"], 0)
        b = cand_deltas.get(cfg["anchor_b"], 0)
        old_m = _prev_county_method[race].get(county, {})
        new_m = new_methods.get(county, {})
        mk = _best_method(new_m, old_m)
        entry = {"ts": ts, "race": race, "county": county, "delta": delta, "candDeltas": cand_deltas,
                 "marginDelta": a - b, "methodKey": mk, "methodLabel": METHOD_LABELS.get(mk) if mk else None}
        feed = [entry] + feed
        print(f"[Drop:{race}] {county} +{delta:,} ({mk or 'method pending'}) — {cfg['anchor_a']} +{a}, {cfg['anchor_b']} +{b}")
        if mk:
            _prev_county_method[race][county] = dict(new_m)
        else:
            _unresolved[race][county] = {"snap": dict(old_m), "ts": ts, "cycles": 0}
        _prev_county[race][county] = dict(cands)

    feeds[race] = feed[:60]
    stored_state.setdefault("_drop", {})[race] = {
        "prevCountyMethod": _prev_county_method[race], "unresolved": _unresolved[race]}
    _write_json(STATE_FILE, stored_state)


# ══════════════════════════════════════════════════════════════════════════════
#  AP SCRAPE
# ══════════════════════════════════════════════════════════════════════════════

def _county_from_ap(key, unit):
    fips = str(unit.get("fipsCode") or unit.get("fips") or key or "")
    if fips in FIPS_TO_COUNTY:
        return FIPS_TO_COUNTY[fips]
    name = str(unit.get("reportingunitName") or unit.get("name") or "").strip()
    n = name.lower().replace(".", "")
    if n in ("baltimore city",):
        return "Baltimore City"
    if n in ("baltimore", "baltimore county"):
        return "Baltimore County"
    n = re.sub(r"\s+county$", "", n).replace("st mary's", "saint mary's")
    for c in COUNTIES.values():
        if c.lower() == n:
            return c
    return None


def _classify_ap_meta(meta, race_id):
    """Decide whether an AP race metadata blob is the Governor race or Question 3."""
    forced = url_overrides.get("apRaceIds") or {}
    for k, rid in forced.items():
        if rid and rid == race_id:
            return k
    def g(*ks):
        for k in ks:
            v = meta.get(k) if isinstance(meta, dict) else None
            if v:
                return str(v)
        return ""
    office = g("officeName", "office", "officeID", "officeTypeName").lower()
    seat   = g("seatName", "seat", "seatNum", "raceName", "description", "title", "name").lower()
    if office in ("g",) or ("governor" in office and "lieutenant" not in office):
        return "gov"
    is_measure = office in ("i",) or any(w in office for w in ("ballot", "measure", "question", "initiative", "amendment", "referendum"))
    if is_measure:
        if re.search(r"(question|measure|amendment|q)\s*#?\s*3\b", seat) or seat.strip() in ("3",):
            return "q3"
        if "redistrict" in json.dumps(meta).lower() or "electoral district" in json.dumps(meta).lower():
            return "q3"
    return None


def parse_ap_captured(captured):
    """captured: {url: body}. Returns {race: {eevp:{county:pct}, total:{county:n}, statewide:{...}}}."""
    race_ids = {}  # race_key -> AP race id
    seen = {}
    for url, body in captured.items():
        m = re.search(r"/races/MD/(\d{8}MD\w+?)/metadata\.json", url)
        if not m:
            continue
        rid = m.group(1)
        if AP_EVENT_DATE and not rid.startswith(AP_EVENT_DATE):
            continue
        try:
            meta = json.loads(body)
        except Exception:
            continue
        key = _classify_ap_meta(meta, rid)
        seen[rid] = {"classifiedAs": key,
                     "office": str(meta.get("officeName") or meta.get("officeID") or "")[:60],
                     "seat": str(meta.get("seatName") or meta.get("description") or "")[:80]}
        if key and key not in race_ids:
            race_ids[key] = rid
    for k, rid in (url_overrides.get("apRaceIds") or {}).items():
        if rid:
            race_ids[k] = rid
    _ap_debug["races_seen"].update(seen)

    results = {}
    for key, rid in race_ids.items():
        for url, body in captured.items():
            if f"/races/MD/{rid}/detail.json" not in url:
                continue
            try:
                data = json.loads(body)
            except Exception:
                continue
            units = data.get("reportingUnits") if isinstance(data, dict) and isinstance(data.get("reportingUnits"), list) else None
            items = [(u.get("fipsCode") or u.get("reportingunitID"), u) for u in units] if units else \
                    (list(data.items()) if isinstance(data, dict) else [])
            eevp, total, statewide, expected = {}, {}, {}, {}

            def _expected(unit):
                """AP's expected vote by method: parameters.vote.expected.voteTypes"""
                ex = ((unit.get("parameters") or {}).get("vote") or {}).get("expected") or {}
                vt = ex.get("voteTypes") or {}
                if not ex:
                    return None
                return {"total": ex.get("actual") or 0, "mail": vt.get("absenteeMail") or 0,
                        "early": vt.get("advanceInPerson") or 0, "ed": vt.get("electionDayInPerson") or 0,
                        "provisional": vt.get("provisional") or 0,
                        "eevpByMethod": {"mail": unit.get("eevpAbsenteeMail"), "early": unit.get("eevpAdvanceInPerson"),
                                         "ed": unit.get("eevpEdayInPerson"), "provisional": unit.get("eevpProvisional")}}
            for ukey, unit in items:
                if not isinstance(unit, dict):
                    continue
                cands = unit.get("candidates") or []
                tv = sum((c.get("voteCount") or 0) for c in cands if isinstance(c, dict))
                ev = unit.get("eevp")
                lvl = str(unit.get("reportingunitLevel") or unit.get("level") or "")
                nm = str(unit.get("reportingunitName") or "")
                if str(ukey) in ("24", "MD", "state") or lvl in ("1", "state") or nm in ("Maryland", "MD"):
                    statewide = {"eevp": float(ev) if ev is not None else None, "total": tv, "expected": _expected(unit)}
                    continue
                county = _county_from_ap(ukey, unit)
                if not county or county in eevp:
                    continue
                if ev is not None:
                    eevp[county] = float(ev)
                    ex = _expected(unit)
                    if ex:
                        expected[county] = ex
                    total[county] = tv
            if eevp or statewide:
                results[key] = {"eevp": eevp, "total": total, "statewide": statewide, "expected": expected, "raceId": rid}
            break
    return results


def scrape_ap():
    if not PLAYWRIGHT_OK:
        print("[AP] Playwright not installed — skipping AP.")
        return
    urls = []
    for r in [k for k in RACE_KEYS if not RACES[k].get("light")]:
        u = ap_url(r)
        if u and u not in urls:
            urls.append(u)
    if not urls:
        return
    captured = {}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
            for u in urls:
                page = browser.new_page(viewport={"width": 1280, "height": 900})

                def on_response(resp):
                    try:
                        if "apelections.org" in resp.url or "/races/MD/" in resp.url:
                            captured[resp.url.split("?")[0]] = resp.text()
                    except Exception:
                        pass
                page.on("response", on_response)
                try:
                    page.goto(u, wait_until="domcontentloaded", timeout=30000)
                    for frac in (0.25, 0.5, 0.75, 1.0):
                        page.evaluate(f"window.scrollTo(0, document.body.scrollHeight * {frac})")
                        page.wait_for_timeout(2000)
                    _ap_debug["urls"][u] = "ok"
                except Exception as e:
                    _ap_debug["urls"][u] = f"error: {e}"
                page.close()
            browser.close()
    except Exception as e:
        _ap_debug["last_error"] = str(e)
        print(f"[AP] Browser error: {e}")
        return

    res = parse_ap_captured(captured)
    ts = _now_eastern("%-I:%M:%S %p ET")
    with _lock:
        for key, r in res.items():
            out = latest["races"][key]
            out["apEevp"] = r["eevp"]
            out["apTotalVotes"] = r["total"]
            out["apStatewide"] = r["statewide"]
            out["apExpected"] = r.get("expected", {})
            out["apTs"] = ts
        if res:
            latest["apTs"] = ts
    if res:
        print(f"[AP {ts}] " + " | ".join(f"{k}: {len(v['eevp'])} counties, statewide {v['statewide'].get('eevp')}%"
                                         for k, v in res.items()))
    else:
        print(f"[AP] No MD race data captured ({len(captured)} AP files seen). Check AP URLs in /debug.")


# ══════════════════════════════════════════════════════════════════════════════
#  LOOPS
# ══════════════════════════════════════════════════════════════════════════════
_last_county_method = 0.0


def sboe_cycle():
    global _last_county_method
    log = []
    discover_base(log)
    do_cm = time.time() - _last_county_method >= COUNTY_METHOD_REFRESH_SECS
    for race in RACE_KEYS:
        label = RACES[race]["label"]
        for fn in (scrape_method_page, scrape_county_page):
            try:
                fn(race, log)
            except NotFound:
                log.append(f"⏳ [{label}] {fn.__name__.replace('scrape_', '').replace('_', ' ')}: not posted yet (404)")
            except Exception as e:
                log.append(f"❌ [{label}] {fn.__name__}: {e}")
        if do_cm and not RACES[race].get("light"):
            try:
                scrape_county_methods(race, log)
            except Exception as e:
                log.append(f"❌ [{label}] by-county pages: {e}")
        latest["races"][race]["ts"] = _now_eastern("%-I:%M:%S %p ET")
    if do_cm:
        _last_county_method = time.time()
    with _lock:
        for race in RACE_KEYS:
            if RACES[race].get("light"):
                continue
            try:
                detect_drops(race)
            except Exception as e:
                log.append(f"❌ drop detection [{race}]: {e}")
        latest["ts"] = _now_eastern("%-I:%M:%S %p ET")
        latest["log"] = log
    print(f"[{latest['ts']}] " + " | ".join(log))


def sboe_loop():
    while True:
        try:
            sboe_cycle()
        except Exception as e:
            print(f"SBOE loop error: {e}")
        time.sleep(SBOE_REFRESH_SECS)


def ap_loop():
    time.sleep(8)
    while True:
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                fut = ex.submit(scrape_ap)
                try:
                    fut.result(timeout=120)
                except concurrent.futures.TimeoutError:
                    print("[AP] Scrape timed out after 120s — skipping cycle")
        except Exception as e:
            print(f"[AP] Loop error: {e}")
        time.sleep(AP_REFRESH_SECS)


# ══════════════════════════════════════════════════════════════════════════════
#  HTTP SERVER
# ══════════════════════════════════════════════════════════════════════════════
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler


class Handler(BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _send_json(self, obj, status=200):
        body = obj if isinstance(obj, (bytes, bytearray)) else json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _authed(self):
        return self.headers.get("X-Operator-Password", "") == OPERATOR_PASSWORD

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/tracker", "/index.html"):
            try:
                with open(TRACKER_HTML, "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(body)
            except FileNotFoundError:
                self._send_json(latest)
        elif path == "/results":
            with _lock:
                payload = json.dumps(latest).encode()
            self._send_json(payload)
        elif path == "/state":
            self._send_json(stored_state or {})
        elif path == "/verify":
            self._send_json({"ok": self._authed()})
        elif path == "/health":
            self._send_json({"ok": True})
        elif path == "/debug":
            info = {
                "sboeBase": _working_base, "sboeBasesTried": SBOE_BASES,
                "urls": {r: {"method": race_url(r, "method"), "county": race_url(r, "county"),
                             "byCountyExample": by_county_url(r, 16), "ap": ap_url(r)} for r in RACE_KEYS},
                "urlOverrides": url_overrides,
                "ap": _ap_debug,
                "playwright": PLAYWRIGHT_OK,
                "lastLog": latest.get("log"),
                "methodByRace": {r: latest["races"][r]["method"] for r in RACE_KEYS},
                "countiesWithMethodData": {r: sorted(latest["races"][r]["countyMethod"].keys()) for r in RACE_KEYS},
                "apCounties": {r: latest["races"][r]["apEevp"] for r in RACE_KEYS},
                "unresolvedDrops": {r: {k: {"ts": v.get("ts"), "cycles": v.get("cycles")} for k, v in _unresolved[r].items()} for r in RACE_KEYS},
                "files": {f: (f"{round(os.path.getsize(f)/1024, 1)} KB" if os.path.exists(f) else "missing")
                          for f in (STATE_FILE, CONFIG_FILE, URLS_FILE)},
            }
            self._send_json(json.dumps(info, indent=2).encode())
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        global stored_state, stored_config, _working_base
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b"{}"
        path = self.path.split("?")[0]
        try:
            data = json.loads(body or b"{}")
        except Exception:
            self._send_json({"error": "bad json"}, 400)
            return

        if path == "/state":
            if not self._authed():
                self._send_json({"error": "unauthorized"}, 403); return
            with _lock:
                keep_feeds = stored_state.get("voteFeeds")
                keep_drop  = stored_state.get("_drop")
                stored_state = data if isinstance(data, dict) else {}
                if keep_feeds is not None:
                    stored_state["voteFeeds"] = keep_feeds
                if keep_drop is not None:
                    stored_state["_drop"] = keep_drop
                _write_json(STATE_FILE, stored_state)
            self._send_json({"ok": True})

        elif path == "/config":
            stored_config = data
            latest["config"] = stored_config
            _write_json(CONFIG_FILE, stored_config)
            self._send_json({"ok": True})

        elif path == "/urls":
            if not self._authed():
                self._send_json({"error": "unauthorized"}, 403); return
            # body: { base?, gov?:{method,county,by_county,ap}, q3?:{...}, apRaceIds?:{gov,q3} }  ("" clears a value)
            for k, v in data.items():
                if isinstance(v, dict):
                    cur = url_overrides.setdefault(k, {})
                    for kk, vv in v.items():
                        if vv in (None, ""):
                            cur.pop(kk, None)
                        else:
                            cur[kk] = str(vv).strip()
                elif v in (None, ""):
                    url_overrides.pop(k, None)
                else:
                    url_overrides[k] = str(v).strip()
            if "base" in data:
                _working_base = url_overrides.get("base") or None
            _write_json(URLS_FILE, url_overrides)
            self._send_json({"ok": True, "urlOverrides": url_overrides})

        elif path == "/patch-feed":
            if not self._authed():
                self._send_json({"error": "unauthorized"}, 403); return
            race = data.get("race", "gov")
            feed = stored_state.get("voteFeeds", {}).get(race, [])
            changed = 0
            for p in data.get("patches", []):
                i = p.get("index")
                if i is not None and 0 <= i < len(feed):
                    feed[i]["methodKey"] = p.get("methodKey")
                    feed[i]["methodLabel"] = p.get("methodLabel")
                    changed += 1
            _write_json(STATE_FILE, stored_state)
            self._send_json({"ok": True, "changed": changed})
        else:
            self._send_json({"error": "not found"}, 404)

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    print("=" * 64)
    print("  Maryland 2026 General — Governor + Question 3 scraper")
    print(f"  Tracker + data → http://0.0.0.0:{PORT}   (JSON at /results)")
    print(f"  SBOE every {SBOE_REFRESH_SECS}s (by-county pages every {COUNTY_METHOD_REFRESH_SECS}s)")
    print(f"  AP   every {AP_REFRESH_SECS}s → {ap_url('gov')}  |  {ap_url('q3')}")
    if not PLAYWRIGHT_OK:
        print("  ⚠️  Playwright not installed — AP % reporting disabled.")
    print("=" * 64)
    threading.Thread(target=sboe_loop, daemon=True).start()
    if os.environ.get("DISABLE_AP") != "1":
        threading.Thread(target=ap_loop, daemon=True).start()
    try:
        ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\nScraper stopped.")
