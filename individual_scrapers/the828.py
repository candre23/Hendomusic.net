#!/usr/bin/env python3
"""
Scrape live music events from The 828 events page and write them in Hendomusic text format.

Usage:
    python the828.py --start 2026-05-27 --end 2026-06-03 --output 828_events.txt

Dependencies:
    pip install requests beautifulsoup4
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from html import unescape
from pathlib import Path
from typing import Iterable

import requests
from bs4 import BeautifulSoup, Tag

EVENTS_URL = "https://www.the828taproom.com/events"
VENUE_NAME = "THE 828"

WEEKDAYS = "Monday Tuesday Wednesday Thursday Friday Saturday Sunday".split()
MONTHS = "January February March April May June July August September October November December".split()

DATE_RE = re.compile(
    rf"^({'|'.join(WEEKDAYS)}),\s+({'|'.join(MONTHS)})\s+(\d{{1,2}}),\s+(\d{{4}})$"
)

# Fallback only. The primary parser reads Squarespace's separate start/end <time> tags.
TIME_RE = re.compile(
    r"(\d{1,2}:\d{2})\s*([AP]M)\s*(?:[-–—]|to)?\s*(\d{1,2}:\d{2})\s*([AP]M)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Event:
    event_date: date
    title: str
    start_time: str
    end_time: str
    sort_minutes: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scrape The 828 live music events into Hendomusic plaintext format."
    )
    parser.add_argument("--start", required=True, help="Start date, inclusive, YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="End date, inclusive, YYYY-MM-DD")
    parser.add_argument("--output", required=True, help="Output text file path")
    parser.add_argument(
        "--url",
        default=EVENTS_URL,
        help=f"Events page URL. Default: {EVENTS_URL}",
    )
    parser.add_argument(
        "--include-all-events",
        action="store_true",
        help="Include non-Live Music listings too. By default only Live Music events are written.",
    )
    return parser.parse_args()


def parse_iso_date(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Invalid date {value!r}; use YYYY-MM-DD") from exc


def fetch_html(url: str) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36"
        )
    }
    response = requests.get(url, headers=headers, timeout=30)
    response.raise_for_status()
    return response.text


def clean_text(value: str) -> str:
    value = unescape(value)
    value = value.replace("\u202f", " ")  # narrow no-break space, common before AM/PM
    value = value.replace("\xa0", " ")    # no-break space
    value = value.replace("\u2009", " ")  # thin space
    value = value.replace("\u2019", "'")
    value = value.replace("\u2018", "'")
    value = value.replace("\u201c", '"')
    value = value.replace("\u201d", '"')
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def parse_site_date_from_tag(date_tag: Tag) -> date | None:
    iso_value = date_tag.get("datetime", "")
    if iso_value:
        try:
            return datetime.strptime(iso_value[:10], "%Y-%m-%d").date()
        except ValueError:
            pass

    text = clean_text(date_tag.get_text(" ", strip=True))
    match = DATE_RE.match(text)
    if not match:
        return None

    _, month_name, day, year = match.groups()
    return datetime.strptime(f"{month_name} {day} {year}", "%B %d %Y").date()


def parse_clock_text(time_text: str) -> tuple[str, int] | None:
    """
    Convert '6:00 PM' to ('6:00', 1080).

    The displayed output intentionally drops AM/PM to match Hendomusic format.
    The minutes value is retained for accurate sorting.
    """
    time_text = clean_text(time_text).upper()
    match = re.match(r"^(\d{1,2}):(\d{2})\s*([AP]M)$", time_text)
    if not match:
        return None

    hour_text, minute_text, ampm = match.groups()
    hour = int(hour_text)
    minute = int(minute_text)

    if ampm == "AM":
        sort_hour = 0 if hour == 12 else hour
    else:
        sort_hour = 12 if hour == 12 else hour + 12

    return f"{hour}:{minute:02d}", sort_hour * 60 + minute


def parse_structured_time_range(card: Tag) -> tuple[str, str, int] | None:
    """
    Primary parser for Squarespace event cards.

    Expected HTML:
      <time class="event-time-localized-start">6:00 PM</time>
      <time class="event-time-localized-end">9:00 PM</time>
    """
    start_tag = card.select_one("time.event-time-localized-start")
    end_tag = card.select_one("time.event-time-localized-end")

    if not start_tag or not end_tag:
        return None

    start_parsed = parse_clock_text(start_tag.get_text(" ", strip=True))
    end_parsed = parse_clock_text(end_tag.get_text(" ", strip=True))

    if not start_parsed or not end_parsed:
        return None

    start_out, sort_minutes = start_parsed
    end_out, _ = end_parsed
    return start_out, end_out, sort_minutes


def parse_fallback_time_range(text: str) -> tuple[str, str, int] | None:
    match = TIME_RE.search(clean_text(text))
    if not match:
        return None

    start_hm, start_ampm, end_hm, end_ampm = match.groups()
    start_parsed = parse_clock_text(f"{start_hm} {start_ampm}")
    end_parsed = parse_clock_text(f"{end_hm} {end_ampm}")

    if not start_parsed or not end_parsed:
        return None

    start_out, sort_minutes = start_parsed
    end_out, _ = end_parsed
    return start_out, end_out, sort_minutes


def find_event_card(title_tag: Tag) -> Tag:
    """
    Return the smallest useful parent containing the title, date, and time tags.

    Squarespace has changed wrapper class names over time, so this avoids depending
    on a single parent class like article.eventlist-event.
    """
    current: Tag | None = title_tag

    while current is not None and isinstance(current, Tag):
        has_title = current.select_one(".eventlist-title")
        has_date = current.select_one("time.event-date")
        has_start = current.select_one("time.event-time-localized-start")
        has_end = current.select_one("time.event-time-localized-end")

        if has_title and has_date and has_start and has_end:
            return current

        current = current.parent if isinstance(current.parent, Tag) else None

    return title_tag.parent if isinstance(title_tag.parent, Tag) else title_tag


def card_is_live_music(card: Tag) -> bool:
    # Category is usually shown somewhere in the same event card.
    return "live music" in clean_text(card.get_text(" ", strip=True)).lower()


def extract_events(html: str, include_all_events: bool = False) -> list[Event]:
    soup = BeautifulSoup(html, "html.parser")
    events: list[Event] = []

    title_tags = soup.select("h1.eventlist-title")
    if not title_tags:
        title_tags = soup.select(".eventlist-title")

    for title_tag in title_tags:
        card = find_event_card(title_tag)

        title_link = title_tag.select_one("a.eventlist-title-link")
        title_source = title_link if title_link else title_tag
        title = clean_text(title_source.get_text(" ", strip=True))

        if not title:
            continue

        if not include_all_events and not card_is_live_music(card):
            continue

        date_tag = card.select_one("time.event-date")
        if not date_tag:
            print(f"Warning: skipping {title!r}; could not find event date", file=sys.stderr)
            continue

        event_date = parse_site_date_from_tag(date_tag)
        if not event_date:
            print(f"Warning: skipping {title!r}; could not parse event date", file=sys.stderr)
            continue

        time_range = parse_structured_time_range(card)

        # Safety fallback: if Squarespace changes the class names but leaves visible text,
        # try to parse a combined time range from the card text.
        if not time_range:
            time_range = parse_fallback_time_range(card.get_text(" ", strip=True))

        if not time_range:
            print(f"Warning: skipping {title!r} on {event_date}; could not find time range", file=sys.stderr)
            continue

        start_time, end_time, sort_minutes = time_range
        events.append(
            Event(
                event_date=event_date,
                title=title,
                start_time=start_time,
                end_time=end_time,
                sort_minutes=sort_minutes,
            )
        )

    # De-duplicate in case Squarespace repeats mobile/desktop event blocks.
    unique = list(dict.fromkeys(events))
    unique.sort(key=lambda e: (e.event_date, e.sort_minutes, e.title.lower()))
    return unique


def format_date_header(day: date) -> str:
    return day.strftime("%A, %B %d").upper().replace(" 0", " ")


def format_events(events: Iterable[Event]) -> str:
    grouped: dict[date, list[Event]] = defaultdict(list)
    for event in events:
        grouped[event.event_date].append(event)

    chunks: list[str] = []
    for day in sorted(grouped):
        chunks.append("*****")
        chunks.append(format_date_header(day))
        for event in grouped[day]:
            chunks.append(f"{VENUE_NAME}  *  {event.title}  *  {event.start_time} – {event.end_time}")

    return "\n".join(chunks) + ("\n" if chunks else "")


def main() -> int:
    args = parse_args()
    start_date = parse_iso_date(args.start)
    end_date = parse_iso_date(args.end)
    if end_date < start_date:
        raise SystemExit("--end must be on or after --start")

    html = fetch_html(args.url)
    all_events = extract_events(html, include_all_events=args.include_all_events)
    selected = [event for event in all_events if start_date <= event.event_date <= end_date]

    output_text = format_events(selected)
    output_path = Path(args.output)
    output_path.write_text(output_text, encoding="utf-8")

    print(f"Wrote {len(selected)} event(s) to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
