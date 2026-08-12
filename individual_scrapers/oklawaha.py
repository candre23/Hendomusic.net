# oklawaha.py
import argparse, sys, time, re, platform
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, date, timedelta
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup

BASE_URL = "https://oklawahabrewing.com/events/"
VENUE_NAME = "Oklawaha Brewing Company"

MONTHS = ("January","February","March","April","May","June","July",
          "August","September","October","November","December")

DATE_RE = re.compile(
    r"\b(?P<month>January|February|March|April|May|June|July|August|September|October|November|December)\s+"
    r"(?P<day>\d{1,2})(?:,\s*(?P<year>\d{4}))?",
    re.IGNORECASE,
)

TIME_RANGE_RE = re.compile(
    r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)\s*(?:–|-|to)\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)",
    re.IGNORECASE
)
SINGLE_TIME_RE = re.compile(
    r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)",
    re.IGNORECASE
)

CLEAN_SUFFIX_RE = re.compile(
    r"""\s*          # trailing space
        [-–—|:]*\s*  # optional separators before the tag
        (?:acoustic\s+)?  # optional 'acoustic ' prefix
        live\s*(?:@|at)\s*oklawaha!?$   # LIVE @ Oklawaha! (case-insensitive)
    """,
    re.IGNORECASE | re.VERBOSE,
)

def clean_title(title: str) -> str:
    t = CLEAN_SUFFIX_RE.sub("", title).strip()
    t = re.sub(r"\s{2,}", " ", t)
    return t


@dataclass
class Event:
    title: str
    start: datetime
    end: datetime | None

def vprint(verbose, *args): 
    if verbose: print(*args)

def fetch(url, session, verbose=False):
    vprint(verbose, f"[GET] {url}")
    r = session.get(url, timeout=30)
    r.raise_for_status()
    return r.text

def normalize_meta_text(s: str) -> str:
    # Collapse whitespace, replace unicode en dash etc with normal '-', drop '@'
    s = s.replace("–", "-").replace("—", "-").replace("@", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s

def parse_date_part(meta_text: str, default_year: int) -> date | None:
    m = DATE_RE.search(meta_text)
    if not m:
        return None
    month_name = m.group("month")
    day = int(m.group("day"))
    year = int(m.group("year")) if m.group("year") else default_year
    month_num = MONTHS.index(month_name.capitalize()) + 1
    try:
        return date(year, month_num, day)
    except ValueError:
        return None

def parse_time_range(meta_text: str, base_date: date) -> tuple[datetime | None, datetime | None]:
    # Try range first
    m = TIME_RANGE_RE.search(meta_text)
    if m:
        sh, sm, sap, eh, em, eap = m.groups()
        sh, sm = int(sh), int(sm or 0)
        eh, em = int(eh), int(em or 0)
        sap = sap.lower(); eap = eap.lower()
        if sap == "pm" and sh != 12: sh += 12
        if sap == "am" and sh == 12: sh = 0
        if eap == "pm" and eh != 12: eh += 12
        if eap == "am" and eh == 12: eh = 0
        start_dt = datetime(base_date.year, base_date.month, base_date.day, sh, sm)
        end_dt = datetime(base_date.year, base_date.month, base_date.day, eh, em)
        if end_dt < start_dt:
            end_dt += timedelta(days=1)
        return start_dt, end_dt
    # Single time (treat as start only)
    m2 = SINGLE_TIME_RE.search(meta_text)
    if m2:
        hh, mm, ap = m2.groups()
        hh, mm = int(hh), int(mm or 0)
        ap = ap.lower()
        if ap == "pm" and hh != 12: hh += 12
        if ap == "am" and hh == 12: hh = 0
        return datetime(base_date.year, base_date.month, base_date.day, hh, mm), None
    # No time -> default to noon so it at least sorts
    return datetime(base_date.year, base_date.month, base_date.day, 12, 0), None

def fmt_header(d: date) -> str:
    weekday = d.strftime("%A").upper()
    month = d.strftime("%B").upper()
    return f"{weekday}, {month} {d.day}"

def _fmt_clock(dt: datetime) -> str:
    if platform.system().lower().startswith("win"):
        return dt.strftime("%#I:%M")
    return dt.strftime("%-I:%M")

def fmt_timeframe(start_dt: datetime | None, end_dt: datetime | None) -> str:
    if start_dt and end_dt:
        return f"{_fmt_clock(start_dt)} – {_fmt_clock(end_dt)}"
    if start_dt:
        return _fmt_clock(start_dt)
    return ""

def parse_events_from_page(html: str, base_url: str, verbose=False, year_hint: int = None) -> tuple[list[Event], str | None]:
    soup = BeautifulSoup(html, "html.parser")
    events: list[Event] = []

    # Event containers
    containers = soup.select(".tribe-events-calendar-list__event, article.type-tribe_events")
    if not containers:
        # Fallback: try generic rows
        containers = soup.select(".tribe-common-g-row, article")

    for c in containers:
        # Title
        title_el = c.select_one("a.tribe-events-calendar-list__event-title-link, a.tribe-events-calendar-list__event-title")
        if not title_el:
            title_el = c.select_one("h3 a, h2 a, a.tribe-event-url")
        if not title_el:
            continue
        title = " ".join(title_el.get_text(" ", strip=True).split())
        title = clean_title(title)  # <— add this line


        # Find a line with month + time
        texts = []
        for sel in [
            ".tribe-events-calendar-list__event-datetime",
            ".tribe-events-calendar-list__event-date-tag",
            ".tribe-common-b2",
            ".tribe-events-calendar-list__event-details .tribe-common-b2",
        ]:
            for el in c.select(sel):
                t = el.get_text(" ", strip=True)
                if t:
                    texts.append(t)
        # include container text as last resort
        texts.append(c.get_text(" ", strip=True))

        meta_text = None
        for t in texts:
            tt = normalize_meta_text(t)
            if any(m in tt for m in MONTHS) and ((" am" in tt.lower()) or (" pm" in tt.lower()) or " - " in tt):
                meta_text = tt
                break

        if not meta_text:
            vprint(verbose, f"  [skip] no date/time for '{title}'")
            continue

        # Parse date and time
        base_date = parse_date_part(meta_text, default_year=year_hint)
        if not base_date:
            vprint(verbose, f"  [skip] could not parse date in '{meta_text}' for '{title}'")
            continue

        start_dt, end_dt = parse_time_range(meta_text, base_date)
        if not start_dt:
            vprint(verbose, f"  [skip] could not parse time in '{meta_text}' for '{title}'")
            continue

        events.append(Event(title=title, start=start_dt, end=end_dt))

    # Next Events pagination
    next_url = None
    for a in soup.find_all("a", href=True):
        txt = a.get_text(" ", strip=True).lower()
        if "next events" in txt or txt == "next":
            next_url = urljoin(base_url, a["href"])
            break

    return events, next_url

def main():
    ap = argparse.ArgumentParser(description="Scrape Oklawaha Brewing events (website).")
    ap.add_argument("--start", required=True, help="Start date (YYYY-MM-DD)")
    ap.add_argument("--end", required=True, help="End date (YYYY-MM-DD)")
    ap.add_argument("--out", required=True, help="Output file path")
    ap.add_argument("--max-pages", type=int, default=8, help="Max pagination pages to follow")
    ap.add_argument("--year", type=int, default=datetime.now().year, help="Year to assume if not shown on site")
    ap.add_argument("--verbose", action="store_true", help="Verbose logging")
    args = ap.parse_args()

    try:
        start_date = datetime.strptime(args.start, "%Y-%m-%d").date()
        end_date = datetime.strptime(args.end, "%Y-%m-%d").date()
    except ValueError:
        print("Dates must be YYYY-MM-DD.", file=sys.stderr); sys.exit(1)
    if end_date < start_date:
        print("End date must not be before start date.", file=sys.stderr); sys.exit(1)

    session = requests.Session()
    session.headers.update({
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/124.0.0.0 Safari/537.36")
    })

    page_url = BASE_URL
    all_events: list[Event] = []
    pages_visited = 0

    while page_url and pages_visited < args.max_pages:
        pages_visited += 1
        html = fetch(page_url, session, verbose=args.verbose)
        events, next_url = parse_events_from_page(html, base_url=page_url, verbose=args.verbose, year_hint=args.year)
        vprint(args.verbose, f"[PARSE] found {len(events)} events on page {pages_visited}")
        all_events.extend(events)
        page_url = next_url
        time.sleep(0.3)

    # Deduplicate (title + start timestamp)
    seen = set()
    deduped = []
    for ev in all_events:
        key = (ev.title, ev.start.isoformat())
        if key not in seen:
            seen.add(key)
            deduped.append(ev)

    # Filter by date range
    filtered = [e for e in deduped if start_date <= e.start.date() <= end_date]
    vprint(args.verbose, f"[FILTER] in range: {len(filtered)} / total scraped: {len(deduped)}")

    # Group + sort
    by_day = defaultdict(list)
    for ev in filtered:
        by_day[ev.start.date()].append(ev)
    for d in by_day:
        by_day[d].sort(key=lambda e: e.start)
    days_sorted = sorted(by_day.keys())

    # Write output in your exact format
    def _fmt_clock(dt: datetime) -> str:
        return dt.strftime("%#I:%M") if sys.platform == "win32" else dt.strftime("%-I:%M")

    lines = []
    for d in days_sorted:
        lines.append("*****")
        lines.append(fmt_header(d))
        for ev in by_day[d]:
            timeframe = fmt_timeframe(ev.start, ev.end)
            if timeframe:
                lines.append(f"{VENUE_NAME}  *  {ev.title.strip()}  *  {timeframe}")
            else:
                lines.append(f"{VENUE_NAME}  *  {ev.title.strip()}")

    with open(args.out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))

    print(f"Wrote {sum(len(v) for v in by_day.values())} events across {len(days_sorted)} day(s) to {args.out}")

if __name__ == "__main__":
    main()
