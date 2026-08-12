#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup


LIST_URL = "https://www.scotsmanpublic.com/entertainment/"
VENUE_NAME = "THE SCOTSMAN PUBLIC HOUSE"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12
}


@dataclass(frozen=True)
class EventStub:
    title: str
    url: str
    when: date  # authoritative date from /entertainment/


@dataclass(frozen=True)
class EventDetail:
    title: str
    url: str
    when: date
    start: Optional[datetime]
    end: Optional[datetime]


def fetch(url: str, session: requests.Session, timeout: int = 30) -> str:
    r = session.get(url, timeout=timeout)
    r.raise_for_status()
    return r.text


def clean_text(s: str) -> str:
    s = s.replace("\u00a0", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def infer_year_for_month_day(month: int, day: int, start_d: date, end_d: date) -> int:
    """
    If the user requests a single-year range, use that year.
    If the range crosses a year boundary, infer which year the event belongs to
    based on month relative to start month.
    """
    if start_d.year == end_d.year:
        return start_d.year

    # crossing years, e.g. 2025-12-20 .. 2026-01-10
    # months earlier than start month belong to end year, otherwise start year
    return end_d.year if month < start_d.month else start_d.year


def parse_entertainment_page(html: str, page_url: str, start_d: date, end_d: date) -> List[EventStub]:
    soup = BeautifulSoup(html, "html.parser")
    main = soup.find("main") or soup.body or soup

    def pick_card_ancestor(a_tag):
        # Prefer the nearest “card-ish” container
        for tagname in ("article", "li", "section", "div"):
            anc = a_tag.find_parent(tagname)
            if anc:
                # Avoid super huge ancestors (like the whole page)
                txt = clean_text(anc.get_text(" ", strip=True))
                if 20 <= len(txt) <= 2000:
                    return anc
        return a_tag.parent or a_tag

    stubs: List[EventStub] = []
    seen_urls: set[str] = set()

    for a in main.select('a[href*="/event/"]'):
        href = (a.get("href") or "").strip()
        if not href:
            continue
        if not href.startswith("http"):
            href = requests.compat.urljoin(page_url, href)

        # De-dupe
        if href in seen_urls:
            continue
        seen_urls.add(href)

        card = pick_card_ancestor(a)
        card_text = clean_text(card.get_text(" ", strip=True))

        # Find Month Day in the CARD text (not the anchor text)
        m = re.search(
            r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2})\b",
            card_text,
            flags=re.IGNORECASE,
        )
        if not m:
            continue

        month = MONTHS[m.group(1).lower()]
        day = int(m.group(2))
        year = infer_year_for_month_day(month, day, start_d, end_d)
        when = date(year, month, day)

        if when < start_d or when > end_d:
            continue

        # Title: try the anchor text first, otherwise nearest heading in the card
        title = clean_text(a.get_text(" ", strip=True))
        if not title or title.lower() in {"read more", "learn more"}:
            h = card.find(["h1", "h2", "h3", "h4"])
            if h:
                title = clean_text(h.get_text(" ", strip=True))

        # Last fallback: remove the date from the card text and keep the first chunk
        if not title:
            tmp = clean_text(re.sub(m.group(0), "", card_text, flags=re.IGNORECASE))
            title = tmp.split(" Entertainment")[0].strip()

        if not title:
            continue

        stubs.append(EventStub(title=title, url=href, when=when))

    return stubs



def _iter_jsonld_objects(soup: BeautifulSoup):
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = (tag.string or tag.get_text() or "").strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue

        if isinstance(data, list):
            for obj in data:
                if isinstance(obj, dict):
                    yield obj
        elif isinstance(data, dict):
            yield data


def _find_event_jsonld(soup: BeautifulSoup) -> Optional[dict]:
    for obj in _iter_jsonld_objects(soup):
        if obj.get("@type") == "Event":
            return obj
        graph = obj.get("@graph")
        if isinstance(graph, list):
            for g in graph:
                if isinstance(g, dict) and g.get("@type") == "Event":
                    return g
    return None


def _parse_iso_dt(dt_str: str) -> Optional[datetime]:
    dt_str = (dt_str or "").strip()
    if not dt_str:
        return None
    try:
        return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    except Exception:
        return None


def parse_times_from_event_page(html: str, when: date) -> Tuple[Optional[datetime], Optional[datetime]]:
    """
    Prefer JSON-LD Event.startDate/endDate (best, unambiguous).
    Fallback: parse 'H:MM to H:MM' (optionally with am/pm) from visible page text
             and combine with `when` from the entertainment grid.
    """
    soup = BeautifulSoup(html, "html.parser")

    # --- JSON-LD path (works for at least two of your events) ---
    ev = _find_event_jsonld(soup)
    if ev:
        sdt = _parse_iso_dt(ev.get("startDate", ""))
        edt = _parse_iso_dt(ev.get("endDate", ""))
        if sdt:
            return sdt, edt

    # --- Text fallback path (needed for Jenny & The Weazels) ---
    text = clean_text(soup.get_text(" ", strip=True))

    # Matches:
    #   3:00 to 6:00
    #   3:00 PM to 6:00 PM
    #   5:00 to 11:59
    m = re.search(
        r"\b(\d{1,2}):(\d{2})\s*(AM|PM|am|pm)?\s*to\s*(\d{1,2}):(\d{2})\s*(AM|PM|am|pm)?\b",
        text,
    )
    if not m:
        return None, None

    sh, sm, sap = int(m.group(1)), int(m.group(2)), m.group(3)
    eh, em, eap = int(m.group(4)), int(m.group(5)), m.group(6)

    def to_24h(h: int, ap: Optional[str]) -> int:
        if not ap:
            # Heuristic: pub "entertainment" times are overwhelmingly afternoon/evening.
            # Treat 1–11 as PM by default; keep 12 as 12.
            if h == 12:
                return 12
            return h + 12
        ap = ap.lower()
        if ap == "am":
            return 0 if h == 12 else h
        # pm
        return 12 if h == 12 else h + 12

    sh24 = to_24h(sh, sap)
    eh24 = to_24h(eh, eap)

    start_dt = datetime(when.year, when.month, when.day, sh24, sm)
    end_dt = datetime(when.year, when.month, when.day, eh24, em)

    # If the end looks like it crosses midnight, bump to next day
    if end_dt <= start_dt:
        end_dt = end_dt.replace(day=end_dt.day + 1)

    return start_dt, end_dt



def day_header(d: date) -> str:
    return f"{d.strftime('%A').upper()}, {d.strftime('%B').upper()} {d.day}"

def fmt_time_12h(dt: datetime) -> str:
    s = dt.strftime("%I:%M %p")
    return s.lstrip("0")



def write_output(events_by_day: Dict[date, List[EventDetail]], out_path: str) -> None:
    with open(out_path, "w", encoding="utf-8") as f:
        for d in sorted(events_by_day.keys()):
            f.write("*****\n")
            f.write(day_header(d) + "\n")
            for ev in events_by_day[d]:
                if ev.start and ev.end:
                    f.write(f"{VENUE_NAME}  *  {ev.title}  *  {fmt_time_12h(ev.start)} – {fmt_time_12h(ev.end)}\n")
                elif ev.start:
                    f.write(f"{VENUE_NAME}  *  {ev.title}  *  {fmt_time_12h(ev.start)}\n")
                else:
                    f.write(f"{VENUE_NAME}  *  {ev.title}\n")


def scrape(start_d: date, end_d: date) -> Dict[date, List[EventDetail]]:
    session = requests.Session()
    session.headers.update({"User-Agent": UA})

    listing_html = fetch(LIST_URL, session)
    stubs = parse_entertainment_page(listing_html, LIST_URL, start_d, end_d)

    out: Dict[date, List[EventDetail]] = {}
    for stub in stubs:
        try:
            ev_html = fetch(stub.url, session)
        except requests.RequestException:
            continue

        sdt, edt = parse_times_from_event_page(ev_html, stub.when)

        out.setdefault(stub.when, []).append(
            EventDetail(
                title=stub.title,
                url=stub.url,
                when=stub.when,
                start=sdt,
                end=edt,
            )
        )

    # Sort by start time then title
    for d in out:
        out[d].sort(
            key=lambda e: (
                (e.start.hour, e.start.minute) if e.start else (99, 99),
                e.title.lower(),
            )
        )

    return out


def parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Scrape The Scotsman Public House /entertainment/ events.")
    p.add_argument("--start", required=True, help="Start date (YYYY-MM-DD), inclusive")
    p.add_argument("--end", required=True, help="End date (YYYY-MM-DD), inclusive")
    p.add_argument("--out", required=True, help="Output filename (txt)")
    return p.parse_args(argv)


def main(argv: List[str]) -> int:
    args = parse_args(argv)
    try:
        start_d = datetime.strptime(args.start, "%Y-%m-%d").date()
        end_d = datetime.strptime(args.end, "%Y-%m-%d").date()
    except ValueError:
        print("Error: --start/--end must be YYYY-MM-DD", file=sys.stderr)
        return 2

    if end_d < start_d:
        print("Error: --end must be >= --start", file=sys.stderr)
        return 2

    events_by_day = scrape(start_d, end_d)
    write_output(events_by_day, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
