#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import collections
import datetime as dt
import re
import sys
from typing import Iterable, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup

EVENTS_URL = "https://saludaoutfitters.com/saluda-polk-county-saluda-outfitters-events"
VENUE_NAME = "SALUDA OUTFITTERS"

WEEKDAYS = ("Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday")
MONTHS = ("January","February","March","April","May","June","July","August",
          "September","October","November","December")

DATE_RX = re.compile(
    rf"\b({'|'.join(WEEKDAYS)})\s+({'|'.join(MONTHS)})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*(\d{{4}}))?",
    re.IGNORECASE,
)

TIME_RANGE_RX = re.compile(
    r"(?<!\d)(\d{1,2})(?::?(\d{2}))?\s*(am|pm)\s*[-–to]+\s*(\d{1,2})(?::?(\d{2}))?\s*(am|pm)\b",
    re.IGNORECASE,
)

# Example in page text: "7pm-10pm Highway 52 Band Friday, October 10th, 2025"
AGENDA_RX = re.compile(
    rf"(?P<t1>\d{{1,2}}(?::\d{{2}})?\s*(?:am|pm))\s*[-–to]+\s*(?P<t2>\d{{1,2}}(?::\d{{2}})?\s*(?:am|pm))\s+"
    rf"(?P<title>.+?)\s+"
    rf"(?P<dow>{'|'.join(WEEKDAYS)}),\s+(?P<mon>{'|'.join(MONTHS)})\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?,\s+(?P<year>\d{{4}})",
    re.IGNORECASE,
)

NOISE_TITLES = {
    "No events for this month.",
    "Read more",
    "Thanksgiving",
    "Christmas",
    "New Year's Eve",
}

def strip_unicode(s: str) -> str:
    return (
        s.replace("\u2013", "-")
         .replace("\u2014", "-")
         .replace("\xa0", " ")
         .replace("\u2018", "'")
         .replace("\u2019", "'")
         .replace("\u201c", '"')
         .replace("\u201d", '"')
    )

def to_12h_no_ampm(h24: int, m: int) -> str:
    h12 = h24 % 12 or 12
    return f"{h12}:{m:02d}"

def ampm_to_24(h: int, m: int, ap: str) -> Tuple[int,int]:
    ap = ap.lower()
    if ap == "pm" and h != 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    return h, m

def normalize_timerange(t1: str, t2: str) -> str:
    def parse_one(t: str) -> Tuple[int,int,str]:
        m = re.match(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)", t.strip(), re.I)
        h = int(m.group(1))
        m_ = int(m.group(2) or 0)
        ap = m.group(3)
        return h, m_, ap
    h1,m1,ap1 = parse_one(t1)
    h2,m2,ap2 = parse_one(t2)
    H1,M1 = ampm_to_24(h1,m1,ap1)
    H2,M2 = ampm_to_24(h2,m2,ap2)
    return f"{to_12h_no_ampm(H1,M1)} {to_12h_no_ampm(H2,M2)}"

def parse_card_date(text: str, default_year: int) -> Optional[dt.date]:
    m = DATE_RX.search(text)
    if not m:
        return None
    _, mon, day, year = m.groups()
    month_num = MONTHS.index(mon.capitalize()) + 1
    year = int(year) if year else default_year
    return dt.date(year, month_num, int(day))

def find_visible_event_cards(soup: BeautifulSoup) -> List[BeautifulSoup]:
    """
    Be strict: real event cards usually carry eventon/evo classes.
    Only keep cards that contain BOTH a date-like string AND a time range.
    """
    candidates = []
    for sel in (
        "div.evo_eventcard",
        "div.evo_event",
        "article.evo_eventcard",
        "article[class*='evo_']",
    ):
        candidates.extend(soup.select(sel))

    good = []
    for node in candidates:
        txt = " ".join(node.stripped_strings)
        tnorm = strip_unicode(txt)
        if TIME_RANGE_RX.search(tnorm) and DATE_RX.search(tnorm):
            good.append(node)
    return good

def pick_title_from(node: BeautifulSoup) -> Optional[str]:
    for sel in ("h3","h2","h4",".event-title","a[rel='bookmark']","a.event_title","a.evo_event_title"):
        el = node.select_one(sel)
        if el:
            return " ".join(el.stripped_strings)
    return None

def fetch_html(url: str) -> str:
    r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    return r.text

def scrape(start: dt.date, end: dt.date, verbose: bool=False):
    html = fetch_html(EVENTS_URL)
    soup = BeautifulSoup(html, "html.parser")
    text_all = strip_unicode(soup.get_text(" ", strip=True))

    events = set()  # dedupe (date, title, timeframe)

    # 1) Parse the agenda string(s) directly (most reliable on this site).
    for m in AGENDA_RX.finditer(text_all):
        title = m.group("title").strip()
        if title in NOISE_TITLES:
            continue
        mon = m.group("mon").capitalize()
        month_num = MONTHS.index(mon) + 1
        d = dt.date(int(m.group("year")), month_num, int(m.group("day")))
        if not (start <= d <= end):
            continue
        timeframe = normalize_timerange(m.group("t1"), m.group("t2"))
        events.add((d, title, timeframe))

    # 2) Parse visible cards (guarded).
    default_year = start.year
    for card in find_visible_event_cards(soup):
        raw = strip_unicode(" ".join(card.stripped_strings))
        d = parse_card_date(raw, default_year)
        if not d or not (start <= d <= end):
            continue
        tr = TIME_RANGE_RX.search(raw)
        if not tr:
            continue
        t1 = f"{tr.group(1)}:{tr.group(2) or '00'} {tr.group(3)}" if tr.group(2) else f"{tr.group(1)} {tr.group(3)}"
        t2 = f"{tr.group(4)}:{tr.group(5) or '00'} {tr.group(6)}" if tr.group(5) else f"{tr.group(4)} {tr.group(6)}"
        timeframe = normalize_timerange(t1, t2)

        title = pick_title_from(card) or title_from_fallback(raw)
        title = title.strip()
        if title in NOISE_TITLES:
            continue
        events.add((d, title, timeframe))

    # Sort by date then start-time
    def time_key(tf: str):
        m = re.match(r"(\d{1,2}):(\d{2})", tf)
        return (int(m.group(1)) % 12) * 60 + int(m.group(2)) if m else 0

    out = sorted(events, key=lambda e: (e[0], time_key(e[2])))
    return out

def title_from_fallback(text: str) -> str:
    # last resort: drop everything after the time range and before weekday
    a = AGENDA_RX.search(text)
    if a:
        return a.group("title")
    # otherwise, take the first non-noise chunk
    parts = [p for p in text.split() if p.strip()]
    return " ".join(parts[:6])

def short_day_header(d: dt.date) -> str:
    return d.strftime("%a, %b ").replace(" 0", " ") + str(d.day)

def write_output(events: List[Tuple[dt.date,str,str]], out_path: str):
    by_day = collections.OrderedDict()
    for d, title, tf in events:
        by_day.setdefault(d, []).append((title, tf))
    lines = []
    for d, items in by_day.items():
        lines.append("*****")
        lines.append(short_day_header(d))
        for title, tf in items:
            tf_part = f" * {tf}" if tf else ""
            lines.append(f"{VENUE_NAME} * {title}{tf_part}")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

def parse_args(argv: Optional[Iterable[str]] = None):
    p = argparse.ArgumentParser(description="Scrape Saluda Outfitters events.")
    p.add_argument("--start", required=True, help="YYYY-MM-DD")
    p.add_argument("--end", required=True, help="YYYY-MM-DD")
    p.add_argument("--out", required=True, help="Output text filename")
    p.add_argument("--verbose", action="store_true")
    return p.parse_args(argv)

def main(argv: Optional[Iterable[str]] = None):
    args = parse_args(argv)
    try:
        start = dt.date.fromisoformat(args.start)
        end = dt.date.fromisoformat(args.end)
    except ValueError:
        print("Dates must be YYYY-MM-DD.", file=sys.stderr)
        return 2
    if end < start:
        print("--end must be on/after --start.", file=sys.stderr)
        return 2

    events = scrape(start, end, verbose=args.verbose)
    write_output(events, args.out)
    if args.verbose:
        print(f"Wrote {len(events)} unique event(s) to {args.out}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
