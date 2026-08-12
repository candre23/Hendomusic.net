#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse, sys, re, time, html
from datetime import datetime, date
from collections import defaultdict
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag

BASE_URL = "https://185kingst.com/events/"
VENUE_NAME = "185 King St. (Noblebrau)"  # keep your current venue label
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/123.0 Safari/537.36"
}

MONTH_MAP = {"Jan":1,"Feb":2,"Mar":3,"Apr":4,"May":5,"Jun":6,"Jul":7,"Aug":8,"Sept":9,"Sep":9,"Oct":10,"Nov":11,"Dec":12}
SEP = "  *  "

def parse_args():
    p = argparse.ArgumentParser(description="Scrape 185 King St. (Noblebrau) events into livemusic.txt format.")
    p.add_argument("--start", required=True, help="YYYY-MM-DD")
    p.add_argument("--end", required=True, help="YYYY-MM-DD")
    p.add_argument("--out", required=True, help="Output filename")
    p.add_argument("--timeout", type=float, default=20.0)
    p.add_argument("--sleep", type=float, default=0.2)
    p.add_argument("--verbose", action="store_true")
    return p.parse_args()

def get_soup(url, timeout):
    r = requests.get(url, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")

# ---------- NEW: text normalizer ----------
def clean_text(s: str) -> str:
    """
    1) HTML-unescape: &#038; -> &, &amp; -> &
    2) Normalize Unicode punctuation to plain ASCII.
    3) Remove zero-width chars; normalize spaces; strip.
    """
    if not s:
        return ""
    t = html.unescape(s)

    replacements = {
        # dashes / hyphens
        "\u2013": "-",  # en dash
        "\u2014": "-",  # em dash
        "\u2010": "-",  # hyphen
        "\u2011": "-",  # non-breaking hyphen
        "\u2012": "-",  # figure dash
        "\u2212": "-",  # minus sign
        "\u00b7": "-",  # middle dot
        "\u2022": "-",  # bullet
        # quotes
        "\u2018": "'", "\u2019": "'",  # single curly quotes
        "\u201a": "'", "\u201b": "'",  # single low-9 / high-reversed-9
        "\u2032": "'",                 # prime
        "\u201c": '"', "\u201d": '"',  # double curly quotes
        "\u00ab": '"', "\u00bb": '"',  # guillemets
        "\u2033": '"',                 # double prime
        # ellipsis
        "\u2026": "...",
        # spaces and invisibles
        "\u00a0": " ",  # non-breaking space
        "\u2000": " ", "\u2001": " ", "\u2002": " ", "\u2003": " ",
        "\u2004": " ", "\u2005": " ", "\u2006": " ", "\u2007": " ",
        "\u2008": " ", "\u2009": " ", "\u200a": " ",
        "\u200b": "",  "\u200c": "",  "\u200d": "",  # zero-width chars
        "\ufeff": "",  # BOM
    }
    for k, v in replacements.items():
        t = t.replace(k, v)

    # collapse runs of spaces/tabs
    t = re.sub(r"[ \t]+", " ", t)
    return t.strip()
# -----------------------------------------

def normalize_time_label(txt):
    if not txt: return None
    t = txt.strip().lower().replace(".", "").replace(" ", "")
    m = re.search(r"(\d{1,2})(?::(\d{2}))?(am|pm)$", t)
    if m:
        h = int(m.group(1)); mnt = int(m.group(2) or 0); ap = m.group(3)
        if ap == "pm" and h != 12: h += 12
        if ap == "am" and h == 12: h = 0
        disp_h = (h % 12) or 12
        return f"{disp_h}:{mnt:02d}"
    m24 = re.search(r"^(\d{1,2})(?::(\d{2}))?$", t)
    if m24:
        h = int(m24.group(1)); mnt = int(m24.group(2) or 0)
        return f"{h % 12 or 12}:{mnt:02d}"
    return None

def extract_show_time_text(text):
    m = re.search(r"Show:\s*([0-9]{1,2}(?::[0-9]{2})?\s*[ap]m)", text, re.I)
    return normalize_time_label(m.group(1)) if m else None

def to_output_date_header(d):
    return d.strftime("%A, %B %-d").upper() if sys.platform != "win32" else d.strftime("%A, %B %#d").upper()

def parse_month_header(txt):
    txt = txt.strip()
    m = re.match(r"^(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4})$", txt)
    if not m: return None, None
    return datetime.strptime(m.group(1), "%B").month, int(m.group(2))

def parse_list_date(label, year):
    # e.g. "Thu, Sept 25"
    m = re.match(r"^[A-Za-z]{3},\s*([A-Za-z]{3,4})\s+(\d{1,2})$", label.strip())
    if not (m and year): return None
    mon = MONTH_MAP.get(m.group(1)); day = int(m.group(2))
    if not mon: return None
    try: return date(year, mon, day)
    except ValueError: return None

def scrape_events(start_d, end_d, timeout=20.0, sleep_s=0.2, verbose=False):
    soup = get_soup(BASE_URL, timeout)

    # Track current month/year while iterating in document order
    current_month = current_year = None
    events_by_day = defaultdict(list)

    # Sweep month separators and event wrappers in sequence
    stream = soup.find_all(["span","div"], recursive=True)
    for node in stream:
        # Month header like: <span class='rhp-events-list-separator-month'><span>September 2025</span></span>
        if isinstance(node, Tag) and "rhp-events-list-separator-month" in node.get("class", []):
            inner = node.get_text(" ", strip=True)
            mnum, yr = parse_month_header(inner or "")
            if mnum and yr:
                current_month, current_year = mnum, yr
            continue

        # Event wrapper
        if isinstance(node, Tag) and {"eventWrapper","rhpSingleEvent"}.issubset(set(node.get("class", []))):
            # Date label inside the wrapper:
            dl = node.select_one(".singleEventDate")
            date_label = dl.get_text(strip=True) if dl else ""
            ev_date = parse_list_date(date_label, current_year)
            if not ev_date:
                if verbose: sys.stderr.write(f"[warn] could not parse date in wrapper (label='{date_label}')\n")
                continue
            if not (start_d <= ev_date <= end_d):
                continue

            # Artist/title – prefer title attribute on the first .url link
            artist = None
            a_url = node.select_one("a.url[title]")
            if a_url and a_url.has_attr("title"):
                artist = a_url["title"].strip()
            if not artist:
                h2 = node.select_one("h2")
                if h2: artist = h2.get_text(" ", strip=True)

            # Show time
            time_span = node.select_one(".rhp-event__time-text--list")
            show_time = extract_show_time_text(time_span.get_text(" ", strip=True)) if time_span else None

            if verbose and not artist:
                sys.stderr.write(f"[warn] missing performer for {ev_date} (label: '{date_label}')\n")

            # CLEAN HERE
            artist = clean_text(artist or "")
            timeframe = clean_text(show_time or "")

            events_by_day[ev_date].append((VENUE_NAME, artist, timeframe))

            if sleep_s: time.sleep(sleep_s)

    return events_by_day

def write_output(events_by_day, out_path):
    with open(out_path, "w", encoding="utf-8") as f:
        for d in sorted(events_by_day.keys()):
            f.write("*****\n")
            f.write(f"{to_output_date_header(d)}\n")
            day_events = sorted(events_by_day[d], key=lambda x: (x[1].lower(), x[2]))
            for venue, artist, timeframe in day_events:
                # Final safety pass (handles anything that slipped through)
                artist = clean_text(artist)
                timeframe = clean_text(timeframe)
                f.write(SEP.join([VENUE_NAME, artist, timeframe]).rstrip() + "\n")

def main():
    args = parse_args()
    try:
        start_d = datetime.strptime(args.start, "%Y-%m-%d").date()
        end_d   = datetime.strptime(args.end,   "%Y-%m-%d").date()
    except ValueError:
        print("ERROR: Start/End must be YYYY-MM-DD.", file=sys.stderr); sys.exit(2)
    if end_d < start_d:
        print("ERROR: end date is before start date.", file=sys.stderr); sys.exit(2)

    data = scrape_events(start_d, end_d, timeout=args.timeout, sleep_s=args.sleep, verbose=args.verbose)
    write_output(data, args.out)
    print(f"OK: wrote {sum(len(v) for v in data.values())} event(s) across {len(events_by_day:=data)} day(s) -> {args.out}")

if __name__ == "__main__":
    main()
