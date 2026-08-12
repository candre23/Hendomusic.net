#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, date
from html import unescape
from typing import List, Tuple, Optional

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# =========================
# Config
# =========================
VENUE_NAME_OUTPUT = "THE PURPLE ONION"
URLS = [
    "https://purpleonionsaluda.com",
    "https://purpleonionsaluda.com/5744-2",
    "https://purpleonionsaluda.com/events-2/sunday-night-concert-series/",
]
REQUEST_TIMEOUT = (10, 20)  # (connect, read)
UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5 Safari/605.1.15",
]

MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}

# =========================
# Junk filtering patterns
# =========================

# Date-only lines (block these from becoming titles)
DATE_ONLY_PATTERNS = [
    # Weekday, Month Day[, Year]  (handles ordinals)
    r"^(?:mon|tue|wed|thu|thur|fri|sat|sun)(?:day)?\s*,?\s*(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\s+\d{1,2}(?:st|nd|rd|th)?(?:,\s*\d{4})?$",
    # Month Day[, Year]  (handles ordinals)
    r"^(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\s+\d{1,2}(?:st|nd|rd|th)?(?:,\s*\d{4})?$",
    # Day Month [Year]  (handles ordinals like '1st October')
    r"^\d{1,2}(?:st|nd|rd|th)?\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*(?:\s*,?\s*\d{4})?$",
    # Numeric MM/DD[/YY] or MM-DD[-YY]
    r"^\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?$",
    # 'Date November 2, 2025' or 'Date 11/02'
    r"^date\s+(?:\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\s+\d{1,2}(?:st|nd|rd|th)?(?:,\s*\d{4})?)$",
    # Month Year like 'November 2025'
    r"^(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\s+\d{4}$",
]
DATE_ONLY_RES = [re.compile(p, re.I) for p in DATE_ONLY_PATTERNS]

# City/State detection to block location blurbs
CITY_STATE_RE = re.compile(r"^[A-Za-z .'-]+\s*,\s*(?:NC|N\.?C\.?|North\s+Carolina)\b", re.I)
CITY_STATE_NOCOMMA_RE = re.compile(r"^[A-Za-z .'-]+\s+(?:NC|N\.?C\.?)\b", re.I)
VENUE_CITY_WORDS = {"saluda"}

# Obvious junk/nav words
STOP_TITLES = {
    "date", "time", "tickets", "ticket", "details", "buy tickets",
    "october", "november", "december", "price", "welcome", "farm fresh",
}
STOP_TITLE_PATTERNS = [
    r"\b(order online|reservations?)\b",
    r"\b(menu|about|family|catering|event venue|job openings)\b",
    r"\bopen\s*table|opentable\b",
    r"\b(music booking inquiry|gift cards?|contact us|log-?in)\b",
    r"press release",
    r"^\W*$",
    r"^time\s+\d",
    r"^time\s+.*\b(am|pm)\b",
    r"^welcome\b",
]
CURRENCY_RE = re.compile(r"\$\s*\d")

# CTA recognizer to block "Tickets View Events Website", etc.
def is_cta_line(s: str) -> bool:
    st = unescape(s or "").strip().lower()
    if re.fullmatch(r"(tickets?\s+)?view\s+events?\s+website", st):
        return True
    words = re.findall(r"[a-z]+", st)
    cta = {"ticket","tickets","view","events","event","website","web","site",
           "reserve","reservation","rsvp","book","booking","info","details",
           "learn","more","menu","buy","purchase","order","open","table","opentable"}
    return sum(1 for w in words if w in cta) >= 2

# =========================
# Utilities & Fetch
# =========================
def vprint(verbose: bool, *args, **kwargs):
    if verbose:
        print(*args, file=sys.stderr, **kwargs)

def make_session(retries=3, backoff=0.5) -> requests.Session:
    sess = requests.Session()
    retry = Retry(
        total=retries, read=retries, connect=retries, status=retries,
        backoff_factor=backoff,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "HEAD"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    sess.mount("https://", adapter)
    sess.mount("http://", adapter)
    return sess

def fetch(url: str, verbose: bool) -> str:
    headers = {
        "User-Agent": UA_POOL[hash(url) % len(UA_POOL)],
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Referer": "https://www.google.com/",
    }
    vprint(verbose, f"[GET] {url}")
    sess = make_session()
    r = sess.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.text

# =========================
# Normalization helpers
# =========================
def normalize_title(s: str) -> str:
    s = unescape(s or "").strip()
    s = re.sub(r"\s+", " ", s)
    s = s.strip(" |•—–-:,")
    return s

def normalize_key(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", normalize_title(title).lower())

def parse_date_flexible(s: str, year_hint: int) -> Optional[date]:
    """
    Gentle date parser. If no year is present, force the year_hint.

    Avoids datetime.strptime() on month/day formats without a year, which
    triggers a DeprecationWarning in newer Python versions and may fail in 3.15+.
    """
    s = normalize_title(s)
    s = re.sub(r"(\d+)(st|nd|rd|th)\b", r"\1", s, flags=re.I)
    s = re.sub(r"^(?:date)\s+", "", s, flags=re.I)

    # 1) Formats that include a year are still safe to parse with strptime
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%m/%d/%Y", "%m-%d-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass

    # 2) Month name + day, optional weekday prefix, no year
    # Examples:
    #   "September 25"
    #   "Sep 25"
    #   "Thursday September 25"
    #   "Thursday, September 25"
    m = re.search(
        r"\b(?:mon|tue|wed|thu|thur|fri|sat|sun)(?:day)?[,]?\s+"
        r"([A-Za-z]{3,9})\s+(\d{1,2})\b",
        s,
        flags=re.I,
    )
    if not m:
        m = re.search(r"\b([A-Za-z]{3,9})\s+(\d{1,2})\b", s, flags=re.I)
    if m:
        mon = MONTHS.get(m.group(1).lower())
        day = int(m.group(2))
        if mon:
            try:
                return date(year_hint, mon, day)
            except ValueError:
                return None

    # 3) Numeric month/day with no year
    # Examples:
    #   "9/25"
    #   "09-25"
    m = re.search(r"\b(\d{1,2})[/-](\d{1,2})\b", s)
    if m:
        mon = int(m.group(1))
        day = int(m.group(2))
        try:
            return date(year_hint, mon, day)
        except ValueError:
            return None

    return None

def normalize_time_str(raw: str) -> Optional[str]:
    """
    Convert '7 PM', '7:00pm', '19:30' to 12h 'H:MM' (no am/pm in output).
    """
    if not raw:
        return None
    s = raw.strip().lower().replace(".", "")
    s = s.replace("–", "-").replace("—", "-")
    m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", s)
    if not m:
        m24 = re.search(r"\b(\d{1,2}):(\d{2})\b", s)
        if m24:
            hh, mm = int(m24.group(1)), int(m24.group(2))
            if hh == 0: hh = 12
            elif hh > 12: hh -= 12
            return f"{hh}:{mm:02d}"
        return None
    hh, mm, ampm = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ampm == "pm" and hh < 12: hh += 12
    if ampm == "am" and hh == 12: hh = 0
    if hh == 0: disp = 12
    elif hh > 12: disp = hh - 12
    else: disp = hh
    return f"{disp}:{mm:02d}"

def coerce_12h(s: Optional[str]) -> Optional[str]:
    return normalize_time_str(s) if s else None

def pick_first_timepair(text: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Find (start, end) time near a date/title blob.
    Ignore hours-of-operation text.
    """
    t = unescape(text or "")
    tl = t.lower()
    if "hours of operation" in tl:
        return (None, None)
    # ranges with am/pm
    m = re.search(r"(\d{1,2}(?::\d{2})?\s*(?:am|pm))\s*[-–to]+\s*(\d{1,2}(?::\d{2})?\s*(?:am|pm))", t, flags=re.I)
    if m:
        return (coerce_12h(m.group(1)), coerce_12h(m.group(2)))
    # ranges 24h
    m = re.search(r"(\d{1,2}:\d{2})\s*[-–]+\s*(\d{1,2}:\d{2})", t)
    if m:
        return (coerce_12h(m.group(1)), coerce_12h(m.group(2)))
    # single am/pm
    m = re.search(r"\b(\d{1,2}(?::\d{2})?\s*(?:am|pm))\b", t, flags=re.I)
    if m:
        return (coerce_12h(m.group(1)), None)
    # single 24h
    m = re.search(r"\b(\d{1,2}:\d{2})\b", t)
    if m:
        return (coerce_12h(m.group(1)), None)
    return (None, None)

def choose_event_time(context: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Prefer an evening show time from the context.
    - If we see multiple candidates, prefer those with explicit PM.
    - Ignore pure daytime ranges that end by 5:00 pm (typical lunch hours).
    """
    text = unescape(context or "")
    candidates: List[Tuple[Optional[str], Optional[str], int]] = []  # (start, end, priority)

    # 1) am/pm ranges
    for m in re.finditer(r"(\d{1,2}(?::\d{2})?\s*(?:am|pm))\s*[-–to]+\s*(\d{1,2}(?::\d{2})?\s*(?:am|pm))", text, flags=re.I):
        s, e = m.group(1), m.group(2)
        s12, e12 = coerce_12h(s), coerce_12h(e)
        pr = 2 if ("pm" in s.lower() or "pm" in e.lower()) else 1
        if e12:
            hh, mm = map(int, e12.split(":"))
            if (hh < 5) or (hh == 5 and mm == 0):
                continue
        candidates.append((s12, e12, pr))

    # 2) single am/pm times
    for m in re.finditer(r"\b(\d{1,2}(?::\d{2})?\s*(?:am|pm))\b", text, flags=re.I):
        s = m.group(1)
        s12 = coerce_12h(s)
        pr = 2 if "pm" in s.lower() else 1
        if s12:
            hh, mm = map(int, s12.split(":"))
            if hh < 4:
                continue
        candidates.append((s12, None, pr))

    # 3) 24h ranges
    for m in re.finditer(r"\b(\d{1,2}:\d{2})\s*[-–]+\s*(\d{1,2}:\d{2})\b", text):
        s12, e12 = coerce_12h(m.group(1)), coerce_12h(m.group(2))
        if s12 and e12:
            sh, sm = map(int, s12.split(":")); eh, em = map(int, e12.split(":"))
            if (eh < 5) or (eh == 5 and em == 0):
                continue
        candidates.append((s12, e12, 1))

    # 4) single 24h times
    for m in re.finditer(r"\b(\d{1,2}:\d{2})\b", text):
        s12 = coerce_12h(m.group(1))
        if s12:
            hh, mm = map(int, s12.split(":"))
            if hh < 4:
                continue
            candidates.append((s12, None, 1))

    if not candidates:
        return (None, None)

    def key(c):
        s, e, pr = c
        if s:
            hh, mm = map(int, s.split(":"))
        else:
            hh, mm = (0, 0)
        return (pr, hh, mm)

    best = sorted(candidates, key=key, reverse=True)[0]
    return (best[0], best[1])

# =========================
# Junk filtering helpers
# =========================
def looks_like_hours_blob(s: str) -> bool:
    t = (s or "").lower()
    if "hours" in t and any(w in t for w in ("monday","tuesday","wednesday","thursday","friday","saturday","sunday")):
        return True
    if t.count("~") >= 1 or t.count("&") >= 2:
        return True
    return False

def is_junk_title(s: str) -> bool:
    s_norm = normalize_title(s)
    st = s_norm.lower()

    if not st or len(st) < 3:
        return True

    if st in STOP_TITLES:
        return True
    if CITY_STATE_RE.match(s_norm) or CITY_STATE_NOCOMMA_RE.match(s_norm):
        return True
    if looks_like_hours_blob(st):
        return True
    if CURRENCY_RE.search(st):
        return True
    for pat in STOP_TITLE_PATTERNS:
        if re.search(pat, st, flags=re.I):
            return True
    # Date-like lines (month/day etc.)
    for rx in DATE_ONLY_RES:
        if rx.match(st):
            return True
    # CTA bundles like 'Tickets View Events Website'
    if is_cta_line(st):
        return True
    # overly long or non-word soup
    if len(st) > 140 or st.count(" ") > 25:
        return True
    if not any(len(w) >= 3 and any(c.isalpha() for c in w) for w in st.split()):
        return True
    return False

# =========================
# Parsing
# =========================
def collect_text(soup: BeautifulSoup, tags: List[str]) -> List[str]:
    acc = []
    for el in soup.find_all(tags):
        txt = el.get_text(" ", strip=True)
        if not txt:
            continue
        if len(txt) > 500:
            continue  # drop nav blobs
        acc.append(txt)
    return acc

def parse_jsonld_events(soup: BeautifulSoup, year_hint: int, verbose: bool) -> List[dict]:
    out = []
    for tag in soup.find_all("script", {"type": "application/ld+json"}):
        try:
            data = json.loads(tag.string or "")
        except Exception:
            continue
        blocks = data if isinstance(data, list) else [data]
        for b in blocks:
            if not isinstance(b, dict):
                continue
            if b.get("@type") not in ("Event", "MusicEvent"):
                continue
            name = normalize_title(b.get("name") or "")
            if not name or is_junk_title(name):
                continue
            startDate = b.get("startDate") or b.get("startTime") or ""
            endDate   = b.get("endDate")   or b.get("endTime")   or ""
            dt = None; st = None; en = None
            m = re.match(r"(\d{4}-\d{2}-\d{2})(?:[T ](\d{2}:\d{2}))?", str(startDate))
            if m:
                dt = datetime.strptime(m.group(1), "%Y-%m-%d").date()
                if m.group(2):
                    st = coerce_12h(m.group(2))
            m2 = re.match(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})", str(endDate))
            if m2:
                en = coerce_12h(m2.group(2))
            if dt:
                out.append({"date": dt, "title": name, "start": st, "end": en, "source": "jsonld"})
    return out

def parse_wordpress_like(html: str, year_hint: int, verbose: bool) -> List[dict]:
    """
    Conservative, date-first proximity parser with evening-time bias:
      - Find a date (div/span/hX/p/li allowed).
      - Require a time nearby (+/-6 blocks around the date).
      - Prefer a band-like title just after the date (up to +16); if none, look up to -12 before.
      - Pick time from the TITLE's local window first (fallback to date window).
      - One event per distinct (date + normalized title) per page.
    """
    soup = BeautifulSoup(html, "html.parser")
    page = soup.title.get_text(strip=True) if soup.title else "page"

    events: List[dict] = []
    events += parse_jsonld_events(soup, year_hint, verbose)

    blocks = collect_text(soup, ["h1","h2","h3","h4","p","li","div","span"])
    n_all = len(blocks)
    seen_keys = set()

    def bandy_score(s: str) -> int:
        score = 0
        words = s.split()
        if len(words) >= 2: score += 1
        low = s.lower()
        if "," in s or "&" in s or " and " in low: score += 1
        if any(c.isupper() for c in s) and any(c.islower() for c in s): score += 1
        if re.search(r"\b[A-Z][a-z]+\s+[A-Z][a-z]+\b", s): score += 1
        return score

    for i, raw in enumerate(blocks):
        dt = parse_date_flexible(raw, year_hint)
        if not dt:
            continue

        # Must have a time near the DATE
        date_lo = max(0, i - 6)
        date_hi = min(n_all, i + 7)
        date_ctx = " | ".join(blocks[date_lo:date_hi])
        st_d, en_d = pick_first_timepair(date_ctx)
        if not st_d and not en_d:
            continue

        # Prefer a title AFTER the date (up to +16), else BEFORE (up to -12)
        best_title = None
        best_score = -99
        best_j = None

        for j in range(i+1, min(i+17, n_all)):
            cand = normalize_title(blocks[j])
            if not cand or is_junk_title(cand):
                continue
            sc = bandy_score(cand)
            if sc > best_score:
                best_score, best_title, best_j = sc, cand, j

        if not best_title:
            for j in range(i-1, max(-1, i-13), -1):
                cand = normalize_title(blocks[j])
                if not cand or is_junk_title(cand):
                    continue
                sc = bandy_score(cand)
                if sc > best_score:
                    best_score, best_title, best_j = sc, cand, j

        if best_title:
            # Time: prefer TITLE context first, fall back to DATE context
            t_lo = max(0, best_j - 4)
            t_hi = min(n_all, best_j + 6)
            title_ctx = " | ".join(blocks[t_lo:t_hi])
            t_start, t_end = choose_event_time(title_ctx)
            if not t_start and not t_end:
                t_start, t_end = choose_event_time(date_ctx)

            key = (dt, normalize_key(best_title))
            if key not in seen_keys:
                events.append({
                    "date": dt,
                    "title": best_title,
                    "start": t_start,
                    "end": t_end,
                    "source": "dom",
                })
                seen_keys.add(key)

    if verbose:
        vprint(True, f"[PARSE] {page}: total={len(events)}")
    return events

# =========================
# Orchestration
# =========================
def within(dt: date, start_d: date, end_d: date) -> bool:
    return start_d <= dt <= end_d

def group_and_dedupe(all_events: List[dict], start_date: date, end_date: date) -> dict:
    # filter to range + dedupe across pages by (date+title_key); merge times if needed
    dedup = {}
    for e in all_events:
        dt = e.get("date"); title = e.get("title")
        if not dt or not title:
            continue
        if not within(dt, start_date, end_date):
            continue
        k = (dt, normalize_key(title))
        if k not in dedup:
            dedup[k] = {
                "date": dt,
                "title": normalize_title(title),
                "start": e.get("start"),
                "end": e.get("end"),
            }
        else:
            # merge in missing times
            if not dedup[k].get("start") and e.get("start"): dedup[k]["start"] = e.get("start")
            if not dedup[k].get("end")   and e.get("end"):   dedup[k]["end"]   = e.get("end")
    # group by date
    grouped = defaultdict(list)
    for _, ev in dedup.items():
        grouped[ev["date"]].append(ev)
    # sort
    for d in grouped:
        grouped[d].sort(key=lambda x: (x["title"].lower()))
    return dict(sorted(grouped.items(), key=lambda kv: kv[0]))

def day_header(dt: date) -> str:
    return f"{dt.strftime('%a')}, {dt.strftime('%b')} {dt.day}"

def fmt_line(artist: str, start: Optional[str], end: Optional[str]) -> str:
    if start and end:
        return f"{VENUE_NAME_OUTPUT} * {artist} * {start} {end}"
    elif start:
        return f"{VENUE_NAME_OUTPUT} * {artist} * {start}"
    return f"{VENUE_NAME_OUTPUT} * {artist}"

def write_output(grouped: dict, out_path: str):
    with open(out_path, "w", encoding="utf-8") as f:
        for dt, items in grouped.items():
            f.write("*****\n")
            f.write(f"{day_header(dt)}\n")
            for e in items:
                f.write(fmt_line(e["title"], e.get("start"), e.get("end")) + "\n")

# =========================
# CLI
# =========================
def parse_cli_date(s: str) -> date:
    for fmt in ("%Y-%m-%d","%m/%d/%Y","%m-%d-%Y","%m/%d/%y","%m-%d-%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    m = re.match(r"^(\d{1,2})/(\d{1,2})$", s.strip())
    if m:
        y = datetime.now().year
        return date(y, int(m.group(1)), int(m.group(2)))
    raise ValueError(f"Unrecognized date: {s}")

def scrape_all(start_date: date, end_date: date, verbose: bool) -> dict:
    year_hint = start_date.year
    all_events = []
    for url in URLS:
        html = fetch(url, verbose)
        all_events += parse_wordpress_like(html, year_hint, verbose)
    if verbose:
        vprint(True, f"[SCRAPE] collected raw events: {len(all_events)}")
    grouped = group_and_dedupe(all_events, start_date, end_date)
    if verbose:
        vprint(True, f"[SCRAPE] after filter/dedupe: {sum(len(v) for v in grouped.values())} events across {len(grouped)} days")
    return grouped

def main():
    ap = argparse.ArgumentParser(description="Scrape The Purple Onion (3 sources) and output de-duplicated list.")
    ap.add_argument("--start", required=True, help="Start date (e.g., 2025-10-28)")
    ap.add_argument("--end", required=True, help="End date (inclusive)")
    ap.add_argument("--out", required=True, help="Output filename")
    ap.add_argument("--verbose", action="store_true", help="Verbose logging")
    args = ap.parse_args()

    start_date = parse_cli_date(args.start)
    end_date = parse_cli_date(args.end)
    if end_date < start_date:
        print("End date must be on or after start date", file=sys.stderr)
        sys.exit(2)

    grouped = scrape_all(start_date, end_date, args.verbose)
    write_output(grouped, args.out)
    if args.verbose:
        print(f"Wrote {sum(len(v) for v in grouped.values())} events across {len(grouped)} days to {args.out}", file=sys.stderr)

if __name__ == "__main__":
    main()
