#!/usr/bin/env python3
"""
Fast Cedar Mountain Canteen event scraper.

Scrapes:
    https://cedarmountaincanteen.com/calendar/

Usage:
    py cmcanteen_fast.py --start 2026-06-01 --end 2026-06-30 --out livemusic.txt --verbose

Dependencies:
    py -m pip install requests beautifulsoup4

This version intentionally avoids the old /events/list/page/... archive pagination,
because that path can be slow and noisy on the current site.
"""

import argparse
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, date
from html import unescape
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


BASE_URL = "https://cedarmountaincanteen.com/calendar/"
VENUE_NAME = "Cedar Mountain Canteen"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0 Safari/537.36"
}

MONTH_ABBR_CUSTOM = {
    1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "Jun",
    7: "Jul", 8: "Aug", 9: "Sept", 10: "Oct", 11: "Nov", 12: "Dec"
}
WEEKDAY_ABBR = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}

EVENT_LINE_RE = re.compile(
    r"(?P<month>January|February|March|April|May|June|July|August|September|October|November|December)\s+"
    r"(?P<day>\d{1,2})\s+@\s+"
    r"(?P<start>\d{1,2}:\d{2}\s*(?:am|pm))\s*[-–]\s*"
    r"(?P<end>\d{1,2}:\d{2}\s*(?:am|pm))",
    re.IGNORECASE,
)

# Things on the calendar that are not live music for your list.
EXCLUDE_TITLE_PATTERNS = [
    r"\btrivia\b",
    r"\bgolf\b",
]

# Things that are live music/community music even if the title does not say "Live Music".
MUSIC_TITLE_PATTERNS = [
    r"\bmusic\b",
    r"\bjam\b",
    r"\bjazz\b",
    r"\bbluegrass\b",
    r"\bsong swap\b",
    r"\bpotluck\b",
    r"\bband\b",
    r"\bduo\b",
    r"\btrio\b",
    r"\bpresents\b",
    r"\balbum release\b",
    r"\bfundraiser\b",
]


@dataclass(frozen=True)
class Event:
    event_date: date
    title: str
    timeframe: str


def parse_args():
    parser = argparse.ArgumentParser(description="Scrape Cedar Mountain Canteen music events.")
    parser.add_argument("--start", required=True, help="Start date, YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="End date, YYYY-MM-DD")
    parser.add_argument("--out", required=True, help="Output text file")
    parser.add_argument("--verbose", action="store_true", help="Print diagnostic details")
    parser.add_argument(
        "--include-all",
        action="store_true",
        help="Include all Cedar Mountain Canteen events, not just music-like events.",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=2,
        help="Maximum calendar pages to fetch. Default 2. Set 1 for fastest.",
    )
    return parser.parse_args()


def log(message, verbose):
    if verbose:
        print(message, file=sys.stderr)


def parse_iso_date(value):
    return datetime.strptime(value, "%Y-%m-%d").date()


def clean_text(value):
    value = unescape(value or "")
    value = value.replace("\xa0", " ")
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def is_excluded_title(title):
    t = title.lower()
    return any(re.search(pattern, t, re.IGNORECASE) for pattern in EXCLUDE_TITLE_PATTERNS)


def is_music_title(title):
    t = title.lower()
    return any(re.search(pattern, t, re.IGNORECASE) for pattern in MUSIC_TITLE_PATTERNS)


def normalize_time(value):
    value = clean_text(value).lower()
    value = re.sub(r"\s+", " ", value)
    return value


def format_timeframe(start_time, end_time):
    return f"{normalize_time(start_time)} - {normalize_time(end_time)}"


def format_day_header(d):
    return f"{WEEKDAY_ABBR[d.weekday()]}, {MONTH_ABBR_CUSTOM[d.month]} {d.day}"


def request_page(url, verbose):
    log(f"[GET] {url}", verbose)
    response = requests.get(url, headers=HEADERS, timeout=20)
    response.raise_for_status()
    return response.text


def event_date_from_match(match, assumed_year):
    month = MONTHS[match.group("month").lower()]
    day = int(match.group("day"))
    return date(assumed_year, month, day)


def extract_event_from_article(article, assumed_year, verbose):
    title_el = article.select_one(
        ".tribe-events-calendar-list__event-title a, "
        ".tribe-events-calendar-list__event-title, "
        "h3 a, h3, h2 a, h2"
    )
    title = clean_text(title_el.get_text(" ", strip=True)) if title_el else ""

    # Prefer exact machine-readable time where available.
    time_el = article.select_one("time[datetime]")
    if time_el and time_el.get("datetime"):
        try:
            start_dt = datetime.fromisoformat(time_el["datetime"].replace("Z", "+00:00"))
            event_date = start_dt.date()
        except ValueError:
            event_date = None
    else:
        event_date = None

    block_text = clean_text(article.get_text(" ", strip=True))
    m = EVENT_LINE_RE.search(block_text)

    if not event_date and m:
        event_date = event_date_from_match(m, assumed_year)

    if m:
        timeframe = format_timeframe(m.group("start"), m.group("end"))
    else:
        # Fallback for cases where start/end are in separate <time> tags.
        times = [clean_text(t.get_text(" ", strip=True)) for t in article.select("time")]
        joined = " ".join(times)
        m2 = re.search(
            r"(\d{1,2}:\d{2}\s*(?:am|pm))\s*[-–]?\s*(\d{1,2}:\d{2}\s*(?:am|pm))",
            joined,
            re.IGNORECASE,
        )
        timeframe = format_timeframe(m2.group(1), m2.group(2)) if m2 else ""

    if not title or not event_date:
        log(f"[skip] could not parse article title/date: {block_text[:120]}", verbose)
        return None

    return Event(event_date=event_date, title=title, timeframe=timeframe)


def parse_articles(soup, start_year, verbose):
    articles = soup.select(
        "article.tribe-events-calendar-list__event, "
        ".tribe-events-calendar-list__event"
    )

    events = []
    for article in articles:
        event = extract_event_from_article(article, start_year, verbose)
        if event:
            events.append(event)

    return events


def parse_text_fallback(soup, start_year, verbose):
    """
    Fallback for the simplified text shape currently visible on the page:
        #### Title
        June 19 @ 2:00 pm - 4:00 pm
        Cedar Mountain Canteen ...
    """
    text = soup.get_text("\n")
    lines = [clean_text(line) for line in text.splitlines()]
    lines = [line for line in lines if line]

    events = []

    for i, line in enumerate(lines):
        m = EVENT_LINE_RE.search(line)
        if not m:
            continue

        # Title is usually the nearest non-empty line above the date line.
        title = ""
        for j in range(i - 1, max(-1, i - 8), -1):
            candidate = lines[j]
            if candidate.lower() in {"events", "search", "find events", "list", "month", "day", "photo", "week", "summary"}:
                continue
            if re.match(r"^[A-Z][a-z]{2}\s+\d{1,2}$", candidate):
                continue
            if candidate.startswith("Image:"):
                continue
            title = candidate
            break

        # Venue is usually the next line or two after the date line.
        venue_nearby = " ".join(lines[i + 1:i + 4])
        if VENUE_NAME.lower() not in venue_nearby.lower():
            continue

        try:
            event_date = event_date_from_match(m, start_year)
        except ValueError:
            log(f"[skip] bad date in line: {line}", verbose)
            continue

        events.append(Event(
            event_date=event_date,
            title=title,
            timeframe=format_timeframe(m.group("start"), m.group("end")),
        ))

    return events


def get_next_calendar_url(soup, current_url):
    """
    The old /events/list/page/... archive is noisy and slow.
    Only follow a next link if it stays under /calendar/.
    """
    possible_links = soup.select(
        "a[rel='next'], "
        ".tribe-events-c-nav__next a, "
        "a.tribe-events-c-nav__next, "
        ".tribe-events-c-nav__list-item--next a"
    )

    for link in possible_links:
        href = link.get("href")
        if not href:
            continue
        absolute = urljoin(current_url, href)
        if "/calendar/" in absolute:
            return absolute

    return None


def scrape_events(start_date, end_date, verbose=False, include_all=False, max_pages=2):
    events = []
    seen_urls = set()

    # Starting on the requested start date helps the Events Calendar plugin return
    # the relevant window instead of making us walk from "today".
    url = f"{BASE_URL}?tribe-bar-date={start_date.isoformat()}"

    for page_num in range(max_pages):
        if url in seen_urls:
            break
        seen_urls.add(url)

        html = request_page(url, verbose)
        soup = BeautifulSoup(html, "html.parser")

        page_events = parse_articles(soup, start_date.year, verbose)
        if not page_events:
            page_events = parse_text_fallback(soup, start_date.year, verbose)

        log(f"[info] parsed {len(page_events)} event(s) from page {page_num + 1}", verbose)

        for event in page_events:
            if start_date <= event.event_date <= end_date:
                if not include_all:
                    if is_excluded_title(event.title):
                        continue
                    if not is_music_title(event.title):
                        continue
                events.append(event)

        # If the page already covered dates after the requested end, there is no reason
        # to continue. This is the main speed improvement.
        if page_events and max(e.event_date for e in page_events) >= end_date:
            break

        next_url = get_next_calendar_url(soup, url)
        if not next_url:
            break

        url = next_url

    # Dedupe and sort.
    unique = {}
    for event in events:
        unique[(event.event_date, event.title.lower(), event.timeframe.lower())] = event

    return sorted(unique.values(), key=lambda e: (e.event_date, e.timeframe, e.title.lower()))


def write_output(events, out_path):
    grouped = defaultdict(list)
    for event in events:
        grouped[event.event_date].append(event)

    lines = []
    dates = sorted(grouped)

    for idx, d in enumerate(dates):
        lines.append(format_day_header(d))
        for event in grouped[d]:
            lines.append(f"{VENUE_NAME}  *  {event.title}  *  {event.timeframe}".rstrip())
        if idx < len(dates) - 1:
            lines.append("*****")

    with open(out_path, "w", encoding="utf-8") as f:
        if lines:
            f.write("\n".join(lines) + "\n")
        else:
            f.write("")


def main():
    args = parse_args()

    try:
        start_date = parse_iso_date(args.start)
        end_date = parse_iso_date(args.end)
    except ValueError:
        print("ERROR: --start and --end must be YYYY-MM-DD", file=sys.stderr)
        sys.exit(1)

    if end_date < start_date:
        print("ERROR: --end must be on or after --start", file=sys.stderr)
        sys.exit(1)

    try:
        events = scrape_events(
            start_date,
            end_date,
            verbose=args.verbose,
            include_all=args.include_all,
            max_pages=args.max_pages,
        )
    except requests.RequestException as exc:
        print(f"ERROR: failed to fetch calendar page: {exc}", file=sys.stderr)
        sys.exit(1)

    write_output(events, args.out)
    print(f"OK: wrote {len(events)} event(s) to {args.out}")


if __name__ == "__main__":
    main()
