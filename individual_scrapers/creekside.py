#!/usr/bin/env python3
# creekside.py

import argparse
import re
import sys
from collections import defaultdict
from datetime import datetime, date
from typing import Dict, List, Tuple, Optional

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateparser

URL = "http://www.creeksidemarket.bravesites.com/this-week-at-creekside-after-5"
VENUE_NAME = "Creekside Market & Grill"
DEFAULT_TIMEFRAME = "5:00 – 8:00"

DATE_RE = re.compile(
    r"""
    (?:
       (?P<dow>Mon|Tue|Tues|Wed|Thu|Thur|Fri|Sat|Sun)[a-z]*[, ]*
    )?
    (
        (?P<mon_name>Jan|January|Feb|February|Mar|March|Apr|April|May|Jun|June|Jul|July|Aug|August|Sep|Sept|Oct|October|Nov|November|Dec|December)\.?
        \s*
        (?P<mday>\d{1,2})(?:st|nd|rd|th)?
      |
        (?P<m>\d{1,2})[/-](?P<d>\d{1,2})
    )
    (?:[, ]+(?P<year>\d{4}))?
    """,
    re.IGNORECASE | re.VERBOSE,
)

def clean_text(s: str) -> str:
    return re.sub(r"[ \t]+", " ", s or "").strip()

def format_header(dt: date) -> str:
    return f"{dt.strftime('%a')}, {dt.strftime('%b')} {dt.day}"

def coerce_date_from_match(m: re.Match, default_year: int) -> Optional[date]:
    try:
        if m.group("mon_name"):
            cand = f"{m.group('mon_name')} {m.group('mday')} {m.group('year') or default_year}"
        else:
            cand = f"{m.group('m')}/{m.group('d')}/{m.group('year') or default_year}"
        return dateparser.parse(cand, fuzzy=True).date()
    except Exception:
        return None

def fetch_html(verbose: bool=False) -> BeautifulSoup:
    if verbose: print(f"[GET] {URL}")
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0 CreeksideScraper/1.3"})
    r = s.get(URL, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    # preserve <br> as line breaks
    for br in soup.find_all("br"):
        br.replace_with("\n")
    if verbose: print(f"[GET] {URL} status={r.status_code} bytes={len(r.text)}")
    return soup

def extract_lines(soup: BeautifulSoup, verbose: bool=False) -> List[str]:
    candidates = []
    for sel in ["div.content","article","div#content","div.page","div.wrapper","div#main","main"]:
        for node in soup.select(sel):
            txt = node.get_text("\n")
            txt = clean_text(txt.replace("\r",""))
            if len(txt) > 150:
                candidates.append(txt)
    if not candidates:
        candidates.append(clean_text(soup.get_text("\n").replace("\r","")))
    text = max(candidates, key=len)

    raw_lines = [ln.strip() for ln in text.split("\n")]
    lines: List[str] = []
    for ln in raw_lines:
        if not ln: continue
        parts = re.split(r"(?:\s{2,}| • )", ln)
        for p in parts:
            p = p.strip()
            if p:
                lines.append(re.sub(r"\s*[–—-]\s*", " - ", p))

    if verbose:
        print(f"[info] extracted {len(lines)} lines")
        for i, ln in enumerate(lines[:40]):
            print(f"  [{i:02}] {ln}")
    return lines

def group_by_date_with_inline(lines: List[str], default_year: int, verbose: bool=False):
    buckets: Dict[date, List[str]] = defaultdict(list)
    inline_events: Dict[date, List[str]] = defaultdict(list)
    current_date: Optional[date] = None

    for ln in lines:
        m = DATE_RE.search(ln)
        if m:
            d = coerce_date_from_match(m, default_year)
            if d:
                current_date = d
                # ✅ ensure the date exists in buckets even if it has no extra lines
                _ = buckets[d]  # touches defaultdict to create the key
                if verbose: print(f"[date] {ln} -> {d}")
                # capture inline performer tail
                tail = clean_text(ln[m.end():])
                tail = re.sub(r"^[\s:–—-]+", "", tail)
                if tail:
                    inline_events[d].append(tail)
                continue
        if current_date:
            buckets[current_date].append(ln)

    return buckets, inline_events

def extract_events_for_day(day_lines: List[str], inline_perfs: List[str], verbose: bool=False) -> List[Tuple[str, str]]:
    events: List[Tuple[str, str]] = []
    for p in inline_perfs:
        perf = clean_text(p)
        if perf:
            events.append((perf, DEFAULT_TIMEFRAME))
            if verbose: print(f"  [+inline] {perf} @ {DEFAULT_TIMEFRAME}")

    # optional fallback guesses
    for ln in day_lines:
        s = clean_text(ln)
        if not s: continue
        if re.search(r"^this week|after 5|menu|special|hours", s, re.I):
            continue
        if re.search(r"[A-Za-z]{2,}", s) and len(s) <= 120:
            if all(s.lower() != p.lower() for p, _ in events):
                events.append((s, DEFAULT_TIMEFRAME))
                if verbose: print(f"  [+guess] {s} @ {DEFAULT_TIMEFRAME}")

    # dedupe
    seen = set(); out = []
    for perf, tf in events:
        key = (perf.lower(), tf)
        if key not in seen:
            seen.add(key); out.append((perf, tf))
    return out

def write_output(events_by_day: Dict[date, List[Tuple[str, str]]], out_path: str, verbose: bool=False):
    days = sorted(events_by_day.keys())
    with open(out_path, "w", encoding="utf-8") as fh:
        first = True
        for d in days:
            evs = [e for e in events_by_day[d] if e]
            if not evs: continue
            if not first: fh.write("*****\n")
            first = False
            fh.write(f"{format_header(d)}\n")
            for perf, timeframe in evs:
                fh.write(f"{VENUE_NAME}  *  {perf}  *  {timeframe}\n")
    if verbose:
        total = sum(len(v) for v in events_by_day.values())
        kept_days = sum(1 for v in events_by_day.values() if v)
        print(f"[OK] wrote {total} event(s) across {kept_days} day(s) -> {out_path}")

def main():
    ap = argparse.ArgumentParser(description="Scrape Creekside Market & Grill weekly live music.")
    ap.add_argument("--start", required=True, help="Start date (YYYY-MM-DD)")
    ap.add_argument("--end", required=True, help="End date (YYYY-MM-DD)")
    ap.add_argument("--out", required=True, help="Output filename")
    ap.add_argument("--verbose", action="store_true", help="Verbose logging")
    args = ap.parse_args()

    try:
        start_date = datetime.strptime(args.start, "%Y-%m-%d").date()
        end_date   = datetime.strptime(args.end,   "%Y-%m-%d").date()
    except ValueError:
        print("[error] --start/--end must be YYYY-MM-DD", file=sys.stderr); sys.exit(1)
    if end_date < start_date:
        print("[error] --end must be on/after --start", file=sys.stderr); sys.exit(1)

    soup = fetch_html(verbose=args.verbose)
    lines = extract_lines(soup, verbose=args.verbose)

    default_year = start_date.year  # assume the page’s dates are for this year
    buckets, inline = group_by_date_with_inline(lines, default_year, verbose=args.verbose)

    # ✅ iterate over ALL known dates (inline-only AND bucketed)
    all_dates = sorted(set(buckets.keys()) | set(inline.keys()))
    events_by_day: Dict[date, List[Tuple[str, str]]] = {}
    for d in all_dates:
        if start_date <= d <= end_date:
            day_lines = buckets.get(d, [])
            events = extract_events_for_day(day_lines, inline.get(d, []), verbose=args.verbose)
            if events:
                events_by_day[d] = events

    write_output(events_by_day, args.out, verbose=args.verbose)

if __name__ == "__main__":
    main()
