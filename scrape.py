#!/usr/bin/env python3
"""
scrape.py

Builds a county-style plaintext list of live-music events for a given date range.
Output format is identical to your weekly county list, so your existing convert.py
and artists.csv / venues.csv enrichment continue to work unchanged.

Example:
    python scrape.py --lma --start 2025-09-24 --end 2025-09-30 -o lma.txt
"""

import os, re, sys, json, argparse, datetime as dt
from dataclasses import dataclass
from typing import List, Optional, Dict, Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
import pathlib

TIME_RE = re.compile(r'^\d{1,2}:\d{2}(?:\s*–\s*\d{1,2}:\d{2})?$')  # H:MM or H:MM – H:MM

def load_blacklist(path: str | None):
    """Return {'venues_exact': set[str], 'phrases': list[str]} from a simple txt file."""
    venues_exact, phrases = set(), []
    if not path:
        return {'venues_exact': venues_exact, 'phrases': phrases}
    p = pathlib.Path(path)
    if not p.exists():
        return {'venues_exact': venues_exact, 'phrases': phrases}
    for raw in p.read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if line.lower().startswith('venue:'):
            venues_exact.add(line.split(':',1)[1].strip().lower())
        else:
            phrases.append(line.lower())
    return {'venues_exact': venues_exact, 'phrases': phrases}

def should_skip(venue: str, artist: str, time_str: str, bl: dict) -> bool:
    """Apply filtering rules."""
    v = (venue or '').strip().lower()
    a = (artist or '').strip().lower()
    t = (time_str or '').strip()

    # 1) must have a proper start time
    if not t or not TIME_RE.match(t):
        return True

    # 2) generic venues (expandable)
    generic_venues = {'asheville', 'asheville area'}
    if v in generic_venues:
        return True

    # 3) external blacklist
    if v in bl.get('venues_exact', set()):
        return True
    hay = f"{v} {a}"
    for phrase in bl.get('phrases', []):
        if phrase in hay:
            return True

    return False

# --------------- UI / Session ---------------

SESSION = requests.Session()
SESSION.headers.update({
    # A desktop UA helps WordPress/TEC return full markup
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.8",
})

# --------------- utilities ---------------

WEEKDAY_NAMES = ["MONDAY","TUESDAY","WEDNESDAY","THURSDAY","FRIDAY","SATURDAY","SUNDAY"]
MONTH_NAMES   = ["JANUARY","FEBRUARY","MARCH","APRIL","MAY","JUNE","JULY","AUGUST","SEPTEMBER","OCTOBER","NOVEMBER","DECEMBER"]

def _to_local_date(s: str) -> dt.date:
    return dt.date.fromisoformat(s)

def _in_range(d: dt.datetime, start: dt.date, end: dt.date) -> bool:
    return start <= d.date() <= end

def _dow_month_day(d: dt.date) -> str:
    return f"{WEEKDAY_NAMES[d.weekday()]}, {MONTH_NAMES[d.month-1]} {d.day}"

def _clean(s: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())

def _fmt_hm_12(d: dt.datetime) -> str:
    """Windows-safe 12h time with no leading 0 and no AM/PM (e.g., '6:00')."""
    s = d.strftime('%I:%M')   # '06:00' on Windows
    return s.lstrip('0') or '0:00'

def _parse_iso_loose(s: Optional[str]) -> Optional[dt.datetime]:
    """Accept 'YYYY-MM-DDTHH:MM[:SS][±HH:MM|Z]' or 'YYYY-MM-DD HH:MM'."""
    if not s:
        return None
    s = s.strip().replace('Z', '')
    s = re.sub(r'([+-]\d{2}):?(\d{2})$', r'', s)  # drop TZ offset if present
    m = re.search(r'^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?', s)
    try:
        return dt.datetime.fromisoformat(m.group(0)) if m else None
    except Exception:
        return None

def _time_range_str(start_iso: Optional[str], end_iso: Optional[str], fallback: Optional[str] = None) -> str:
    """Return 'H:MM – H:MM' (en dash) if both; 'H:MM' if only start; else fallback or ''."""
    sdt = _parse_iso_loose(start_iso)
    edt = _parse_iso_loose(end_iso)
    if sdt and edt:
        return f"{_fmt_hm_12(sdt)} – {_fmt_hm_12(edt)}"
    if sdt:
        return _fmt_hm_12(sdt)
    return fallback or ""

# --------------- event model ---------------

@dataclass
class Event:
    date: dt.date
    venue: str
    artist: str
    time_str: str = ""
    notes: str = ""

# --------------- plaintext formatter (county-compatible) ---------------

def format_plaintext(events: List[Event]) -> str:
    if not events:
        return ""
    by_date: Dict[dt.date, List[Event]] = {}
    for ev in events:
        by_date.setdefault(ev.date, []).append(ev)
    out = []
    out.append("*****")
    for date_key in sorted(by_date.keys()):
        out.append(_dow_month_day(date_key))
        for ev in by_date[date_key]:
            line = f"{ev.venue}  *  {ev.artist}  *  {ev.time_str or ''}".rstrip()
            out.append(line)
            if ev.notes:
                out.append(ev.notes.strip())
        out.append("*****")
    # tidy accidental double separators
    return "\n".join(out).replace("\n*****\n*****\n", "\n*****\n")

# ======================================================================
#  SCRAPER A: LiveMusicAsheville.com (TEC v5/v6 aware + pagination)
# ======================================================================

def _extract_times_text(text: str) -> str:
    """
    Pull '6:00 – 8:00' out of visible strings like 'September 24 @ 6:00 pm - 8:00 pm'
    """
    t = " ".join(text.split())
    m = re.search(r'(\d{1,2}:\d{2})\s*(am|pm)?\s*[-–]\s*(\d{1,2}:\d{2})\s*(am|pm)?', t, re.I)
    if m:
        return f"{m.group(1)} – {m.group(3)}"
    m2 = re.search(r'(\d{1,2}:\d{2})\s*(am|pm)\b', t, re.I)
    return m2.group(1) if m2 else ""


def _jsonld_times_from_event_page(url: str) -> tuple[Optional[str], Optional[str]]:
    """Return (start_iso, end_iso) from Event JSON-LD on the event detail page."""
    try:
        r = SESSION.get(url, timeout=20)
        if not (r.ok and 'text/html' in r.headers.get('content-type','')):
            return None, None
        soup = BeautifulSoup(r.text, "html.parser")
        for s in soup.find_all("script", type="application/ld+json"):
            try:
                blob = json.loads(s.string or "null")
            except Exception:
                continue
            items = []
            if isinstance(blob, dict) and blob.get("@type") == "Event":
                items = [blob]
            elif isinstance(blob, dict) and "@graph" in blob and isinstance(blob["@graph"], list):
                items = [x for x in blob["@graph"] if isinstance(x, dict) and x.get("@type") == "Event"]
            elif isinstance(blob, list):
                items = [x for x in blob if isinstance(x, dict) and x.get("@type") == "Event"]
            for ev in items:
                s_iso = ev.get("startDate"); e_iso = ev.get("endDate")
                if s_iso:
                    return s_iso, e_iso
    except Exception:
        pass
    return None, None

# ---------------- LMA (list-view tile parser; no JSON-LD needed) ----------------

from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup
import re, datetime as dt

# optional but recommended: a session with desktop UA
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.8",
})

def _find_next_link(soup: BeautifulSoup, base: str) -> str | None:
    # rel="next" (best), then TEC nav classes, then text fallback
    a = soup.find('a', rel=lambda v: v and 'next' in v.lower())
    if not a:
        a = soup.select_one('.tribe-events-c-nav__next a, a.tribe-events-c-nav__next')
    if not a:
        a = soup.find('a', string=re.compile(r'\bNext\b', re.I))
    return urljoin(base, a.get('href')) if a and a.get('href') else None

def _select_event_tiles(soup: BeautifulSoup):
    """
    TEC list-view tiles. We target the *row* wrapper you pasted plus
    the article/LI fallbacks used on some skins.
    """
    sels = [
        'div.tribe-events-calendar-list__event-row',
        'article.tribe-events-calendar-list__event',
        'li.tribe-events-calendar-list__event',
        'div.tribe-events-calendar-list__event',
    ]
    for sel in sels:
        nodes = soup.select(sel)
        if nodes:
            return nodes
    # loose fallback
    return soup.find_all(lambda tag:
        tag.name in ('div','article','li') and tag.get('class')
        and any('tribe-events-calendar-list__event' in c for c in tag.get('class'))
    )

_time_pat = re.compile(r'(\d{1,2}:\d{2})')   # we only need the H:MM part (no am/pm)

def _tile_to_event(tile) -> tuple[dt.date | None, str, str, str]:
    """
    Parse one tile into (date, venue, artist, time_str). Returns (None, ... ) if no date.
    """
    # DATE (ISO) → <time class="tribe-events-calendar-list__event-datetime" datetime="YYYY-MM-DD">
    date_iso = None
    t = tile.select_one('time.tribe-events-calendar-list__event-datetime[datetime]')
    if not t:
        # some themes keep the date on the left tag
        t = tile.select_one('time.tribe-events-calendar-list__event-date-tag-datetime[datetime]')
    if t and t.has_attr('datetime'):
        try:
            date_iso = dt.date.fromisoformat(t['datetime'][:10])
        except Exception:
            date_iso = None

    # START/END TIMES (visible text)
    start_txt = ""
    end_txt   = ""
    s1 = tile.select_one('.tribe-event-date-start')
    s2 = tile.select_one('.tribe-event-time')
    if s1:
        m = _time_pat.search(s1.get_text(" ", strip=True))
        if m: start_txt = m.group(1)
    if s2:
        m = _time_pat.search(s2.get_text(" ", strip=True))
        if m: end_txt = m.group(1)
    time_str = f"{start_txt} – {end_txt}".strip()
    time_str = time_str.strip(" –")  # handle missing end time

    # ARTIST
    artist = ""
    for sel in [
        '.tribe-events-calendar-list__event-title a',
        '.tribe-events-calendar-list__event-title',
        'h3 a','h3','h2 a','h2'
    ]:
        el = tile.select_one(sel)
        if el:
            artist = el.get_text(" ", strip=True) or (el.get('title') or "").strip()
            break

    # VENUE
    venue = ""
    v = tile.select_one('.tribe-events-calendar-list__event-venue-title')
    if v:
        venue = v.get_text(" ", strip=True)

    return date_iso, venue, artist, time_str
    
def scrape_livemusicasheville(start: dt.date, end: dt.date, debug: bool=False, blacklist: dict | None=None) -> List[Event]:
    if blacklist is None:
        blacklist = {'venues_exact': set(), 'phrases': []}
        
    """
    Crawl LMA list view, clicking 'Next', and extract date/artist/venue/time directly
    from each tile. Filter to [start, end].
    """
    base = "https://livemusicasheville.com/"
    events: List[Event] = []

    # Find a list page that has tiles
    list_paths = ["/calendar/", "/calendars/list/", "/events/", "/list"]
    first_url = None
    for p in list_paths:
        try:
            r = SESSION.get(urljoin(base, p), timeout=20)
            if r.ok and 'text/html' in (r.headers.get('content-type') or ''):
                soup = BeautifulSoup(r.text, "html.parser")
                if _select_event_tiles(soup):
                    first_url = urljoin(base, p)
                    break
        except Exception:
            continue
    if not first_url:
        if debug: print("[lma] no list page found")
        return events

    seen = set()
    url = first_url
    hops = 0
    while url and url not in seen and hops < 80:
        seen.add(url); hops += 1

        try:
            r = SESSION.get(url, timeout=25); r.raise_for_status()
        except Exception:
            if debug: print(f"[lma] failed {url}")
            break

        soup = BeautifulSoup(r.text, "html.parser")
        tiles = _select_event_tiles(soup)
        if debug:
            print(f"[lma] {url} -> {len(tiles)} tiles")

        page_kept = 0
        page_dates: list[dt.date] = []

        for tile in tiles:
            d, venue, artist, time_str = _tile_to_event(tile)
            if not d:
                continue
            if d < start or d > end:
                # keep track so we can stop once we move beyond the window
                page_dates.append(d)
                continue

            # build the event
            ev = Event(
                date=d,
                venue=_clean(venue) or "Asheville",
                artist=_clean(artist) or "Live Music",
                time_str=time_str
            )

            # ⬇️ filter here
            if not should_skip(ev.venue, ev.artist, ev.time_str, blacklist):
                events.append(ev)
                page_kept += 1
            page_dates.append(d)


        if debug:
            print(f"[lma] kept {page_kept} events on page")

        # If all dates on this page are before the window OR after the window,
        # decide whether to keep paging:
        if page_dates and min(page_dates) > end:
            if debug: print("[lma] reached pages beyond end date; stopping")
            break

        url = _find_next_link(soup, base)

    return events


# ======================================================================
#  SCRAPER B: Config-driven venue scraping (+ optional OpenAI assist)
# ======================================================================

try:
    import yaml  # optional dependency for config mode
except Exception:
    yaml = None

def _fetch_json(url: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        r = SESSION.get(url, params=params, timeout=25)
        if r.ok and r.headers.get("content-type","").startswith("application/json"):
            return r.json()
    except Exception:
        return None
    return None

def scrape_venue_conf(v: Dict[str, Any], start: dt.date, end: dt.date) -> List[Event]:
    name = v.get("name") or "UNKNOWN VENUE"
    base = v.get("url")
    mode = (v.get("mode") or "auto").lower()
    events: List[Event] = []

    # 1) The Events Calendar REST
    if mode in ("auto","tribe"):
        ep = v.get("tribe_api",{}).get("endpoint")
        if ep:
            url = urljoin(base, ep) if ep.startswith("/") else ep
        else:
            url = urljoin(base, "/wp-json/tribe/events/v1/events")
        data = _fetch_json(url, {"start_date": str(start), "end_date": str(end)})
        if data and isinstance(data, dict) and "events" in data:
            for item in data["events"]:
                title = _clean(item.get("title"))
                s_iso = item.get("start_date")
                e_iso = item.get("end_date")
                if s_iso:
                    sdt = _parse_iso_loose(s_iso)
                    if sdt and start <= sdt.date() <= end:
                        events.append(Event(date=sdt.date(), venue=name,
                                            artist=title or "Live Music",
                                            time_str=_time_range_str(s_iso, e_iso)))
            if events or mode == "tribe":
                return events

    # 2) JSON‑LD from the venue page
    try:
        r = SESSION.get(base, timeout=25); r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        found_jsonld = False
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                blob = json.loads(script.string or "null")
            except Exception:
                continue
            blocks = []
            if isinstance(blob, dict) and blob.get("@type") == "Event":
                blocks = [blob]
            elif isinstance(blob, dict) and "@graph" in blob:
                blocks = [x for x in blob["@graph"] if isinstance(x, dict) and x.get("@type") == "Event"]
            elif isinstance(blob, list):
                blocks = [x for x in blob if isinstance(x, dict) and x.get("@type") == "Event"]
            for ev in blocks:
                found_jsonld = True
                title = _clean(ev.get("name"))
                s_iso = ev.get("startDate"); e_iso = ev.get("endDate")
                sdt = _parse_iso_loose(s_iso)
                if sdt and start <= sdt.date() <= end:
                    events.append(Event(date=sdt.date(), venue=name,
                                        artist=title or "Live Music",
                                        time_str=_time_range_str(s_iso, e_iso)))
        if found_jsonld and events:
            return events
    except Exception:
        pass

    # 3) Selector mode (CSS)
    if mode in ("auto","selectors"):
        sel = v.get("selectors") or {}
        if sel.get("event"):
            try:
                r = SESSION.get(base, timeout=25); r.raise_for_status()
                soup = BeautifulSoup(r.text, "html.parser")
                for node in soup.select(sel["event"]):
                    title = _clean(node.select_one(sel.get("title","")).get_text(" ", strip=True) if sel.get("title") else "")
                    venue = _clean(node.select_one(sel.get("venue","")).get_text(" ", strip=True) if sel.get("venue") else "") or name
                    start_iso = None
                    end_iso   = None
                    if sel.get("date"):
                        t = node.select_one(sel["date"])
                        if t and t.has_attr("datetime"):
                            start_iso = t["datetime"]
                    if sel.get("end"):
                        t = node.select_one(sel["end"])
                        if t and t.has_attr("datetime"):
                            end_iso = t["datetime"]
                    time_text = _clean(node.select_one(sel.get("time","")).get_text(" ", strip=True) if sel.get("time") else "")
                    notes = _clean(node.select_one(sel.get("notes","")).get_text(" ", strip=True) if sel.get("notes") else "")

                    sdt = _parse_iso_loose(start_iso)
                    if sdt and start <= sdt.date() <= end:
                        events.append(Event(date=sdt.date(), venue=venue,
                                            artist=title or "Live Music",
                                            time_str=_time_range_str(start_iso, end_iso, fallback=time_text),
                                            notes=notes))
            except Exception:
                pass
        if events:
            return events

    # 4) Optional OpenAI assist (wired in main when --openai)
    return events

# --------------- OpenAI assist (optional) ---------------

def openai_extract_events(page_html: str, venue_name: str, start: dt.date, end: dt.date) -> List[Event]:
    """
    Ask OpenAI to normalize arbitrary HTML into our event shape.
    Requires env var OPENAI_API_KEY. Only called when --openai is passed.
    """
    from openai import OpenAI
    client = OpenAI()
    prompt = f"""
You are extracting live-music events for the venue "{venue_name}" between {start} and {end} (inclusive).
Return ONLY JSON with a list under key 'events'. Each event:
  - title
  - date (YYYY-MM-DD)
  - start_time (HH:MM 24h or null)
  - end_time   (HH:MM 24h or null)
  - notes (short trailing info, optional)
HTML follows:
===
{page_html[:120_000]}
==="""
    resp = client.chat.completions.create(
        model=os.environ.get("OPENAI_MODEL","gpt-5-mini"),
        messages=[{"role":"user","content":prompt}],
        response_format={"type":"json_object"},
        temperature=0
    )
    data = json.loads(resp.choices[0].message.content)
    out: List[Event] = []
    for e in data.get("events", []):
        try:
            date = dt.date.fromisoformat(e["date"])
            if start <= date <= end:
                s_iso = f"{e['date']}T{e['start_time']}:00" if e.get("start_time") else None
                e_iso = f"{e['date']}T{e['end_time']}:00"   if e.get("end_time")   else None
                out.append(Event(
                    date=date, venue=venue_name, artist=_clean(e.get("title")),
                    time_str=_time_range_str(s_iso, e_iso),
                    notes=_clean(e.get("notes"))
                ))
        except Exception:
            continue
    return out

# --------------- CLI ---------------

def main():
    ap = argparse.ArgumentParser(description="Scrape Asheville-area live music into county-style plaintext.")
    ap.add_argument("--lma", action="store_true", help="Scrape LiveMusicAsheville.com")
    ap.add_argument("--config", help="YAML file with per-venue scraping rules (optional)")
    ap.add_argument("--start", required=True, help="YYYY-MM-DD")
    ap.add_argument("--end",   required=True, help="YYYY-MM-DD")
    ap.add_argument("-o","--out", default="extra.txt")
    ap.add_argument("--openai", action="store_true", help="Use OpenAI to normalize hard pages (requires OPENAI_API_KEY)")
    ap.add_argument("--debug", action="store_true", help="Print crawl/debug info")
    ap.add_argument("--blacklist", help="Path to blacklist.txt (phrases and/or 'venue: Name')", default=None)

    args = ap.parse_args()
    
    blacklist = load_blacklist(args.blacklist)
    start = _to_local_date(args.start)
    end   = _to_local_date(args.end)

    all_events: List[Event] = []

    if args.lma:
        all_events.extend(
            scrape_livemusicasheville(start, end, debug=args.debug, blacklist=blacklist)
        )


    if args.config:
        if yaml is None:
            print("YAML support not installed. `pip install pyyaml` to use --config.", file=sys.stderr)
        else:
            try:
                with open(args.config, "r", encoding="utf-8") as f:
                    conf = yaml.safe_load(f) or {}
            except Exception as e:
                print(f"Failed to read config: {e}", file=sys.stderr)
                conf = {}
            for v in (conf.get("venues") or []):
                evs = scrape_venue_conf(v, start, end)
                if args.openai and (not evs or (v.get("mode","").lower()=="openai")):
                    try:
                        r = SESSION.get(v["url"], timeout=25); r.raise_for_status()
                        evs = openai_extract_events(r.text, v.get("name") or "UNKNOWN VENUE", start, end)
                    except Exception:
                        pass
                all_events.extend(evs)

    # write plaintext
    with open(args.out, "w", encoding="utf-8") as f:
        hdr = f"LOCAL LIVE MUSIC:  RANGE  {MONTH_NAMES[start.month-1]} {start.day} – {MONTH_NAMES[end.month-1]} {end.day}, {start.year}\n"
        f.write(hdr)
        f.write("Scraped sources\n")
        if all_events:
            f.write(format_plaintext(all_events))
        else:
            f.write("*****\n(No events found in range)\n*****\n")
    print(f"Wrote {args.out} with {len(all_events)} events")

if __name__ == "__main__":
    main()
