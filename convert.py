# generate_live_music.py (v2)
# Updates: handles "VENUE * TWO SHOWS!" blocks and safer venue matching.

import re, os, csv, html, difflib, unicodedata, argparse, calendar
from pathlib import Path
from collections import defaultdict
from typing import List, Dict, Tuple, Optional

MONTHS_FULL = "JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|AUGUST|SEPTEMBER|OCTOBER|NOVEMBER|DECEMBER"
MONTHS_ABBR = "JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|SEPT|OCT|NOV|DEC"
WEEKDAYS_FULL = "MONDAY|TUESDAY|WEDNESDAY|THURSDAY|FRIDAY|SATURDAY|SUNDAY"
WEEKDAYS_ABBR = "MON|TUE|TUES|WED|THU|THUR|FRI|SAT|SUN"

DATE_HDR_RE_FULL = re.compile(
    rf"^\s*({WEEKDAYS_FULL}),\s+({MONTHS_FULL})\s+(\d{{1,2}})\s*$",
    re.I,
)
DATE_HDR_RE_ABBR = re.compile(
    rf"^\s*({WEEKDAYS_ABBR})\s*,\s*({MONTHS_ABBR})\s+(\d{{1,2}})\s*$",
    re.I,
)

SPLIT_TOKEN = "  *  "

VENUE_FIELDS  = ["text_list_name", "html_display_name", "venue_website", "google_maps_link", "town", "venue_image"]

ARTIST_FIELDS = ["artist_name", "artist_link", "artist_image", "artist_style", "artist_display_name"]

GENERIC_VENUE_TOKENS = {
    "the","and","company","co","brewing","brewery","tap","house","taphouse","taproom","bar",
    "beer","garden","grill","pub","vineyard","vineyards","winery","cider","hall","center","place",
    "sports","restaurant","music","venue"
}

VENUE_KEYWORDS_RE = re.compile(
    r"\b(BREW|BREWERY|VINEYARD|BAR|BREWING|THEATRE|THEATER|PLACE|GARDEN|GRILL|CIDER|WINERY|HALL|CENTER|CO\.|CO|PUB|FARMS"
    r"|TAPROOM|CANTEEN|LOUNGE|CLUB|ROOM|INN|TAVERN|SALOON|CAFE|CAFÉ|ST\.|MUSIC HALL|AMPHITHEATER|AMPHITHEATRE)\b",
    re.I
)

_WD_MAP = {
    "mon":"Monday", "tue":"Tuesday", "tues":"Tuesday", "wed":"Wednesday",
    "thu":"Thursday", "thur":"Thursday", "fri":"Friday", "sat":"Saturday", "sun":"Sunday"
}
_MON_MAP = {
    "jan":"January","feb":"February","mar":"March","apr":"April","may":"May","jun":"June",
    "jul":"July","aug":"August","sep":"September","sept":"September","oct":"October","nov":"November","dec":"December",
    "january":"January","february":"February","march":"March","april":"April","june":"June","july":"July",
    "august":"August","september":"September","october":"October","november":"November","december":"December",
}

def _canonical_heading_from_line(line: str) -> str | None:
    """Return 'Weekday, Month Day' if line is a day heading; else None."""
    s = (line or "").strip()
    m = DATE_HDR_RE_FULL.match(s)
    if m:
        wd, mon, day = m.group(1), m.group(2), int(m.group(3))
        return f"{wd.title()}, {mon.title()} {day}"
    m = DATE_HDR_RE_ABBR.match(s)
    if m:
        wd, mon, day = m.group(1).lower(), m.group(2).lower(), int(m.group(3))
        wd_full  = _WD_MAP.get(wd)
        mon_full = _MON_MAP.get(mon)
        if wd_full and mon_full:
            return f"{wd_full}, {mon_full} {day}"
    return None


def norm(s: str) -> str:
    if s is None: return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    rep = {
        "&": " and ",
        " co.": " company",
        " co": " company",
        " brewing co.": " brewing company",
        " brewing co": " brewing company",
        " theatre": " theater",
        " taphouse": " tap house",
        "’": "'",
        "‘": "'",
        "—": "-",
        "–": "-",
    }
    for k,v in rep.items():
        s = s.replace(k,v)
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def token_set(s: str) -> set:
    return set(norm(s).split())

def fuzzy_lookup(name: str, table: Dict[str, dict], *, kind: str, cutoff: float) -> Tuple[str, Optional[dict], bool]:
    """Exact match first; otherwise use difflib but, for venues, require at least one non-generic token overlap."""
    n = norm(name)
    if not n: return "", None, False
    if n in table:
        return n, table[n], True
    if not table:
        return n, None, False

    toks = token_set(name)
    best_key, best_ratio, best_overlap = None, 0.0, 0

    for k in table.keys():
        ktoks = set(k.split())
        overlap = len((toks & ktoks) - (GENERIC_VENUE_TOKENS if kind=="venue" else set()))
        ratio = difflib.SequenceMatcher(None, n, k).ratio()
        if kind == "venue" and overlap == 0:
            continue
        if ratio > best_ratio or (ratio == best_ratio and overlap > best_overlap):
            best_key, best_ratio, best_overlap = k, ratio, overlap

    if best_key and best_ratio >= cutoff:
        return best_key, table[best_key], True
    return n, None, False

def load_csv(path: Path, fields: List[str]) -> Dict[str, dict]:
    data = {}
    if path.exists():
        with path.open(newline="", encoding="utf-8") as f:
            rdr = csv.reader(f)
            for row in rdr:
                if not row or (row and row[0].startswith("#")):
                    continue
                row = row + [""]*(len(fields)-len(row))
                rec = {fields[i]: row[i].strip() for i in range(len(fields))}
                key = norm(rec[fields[0]])
                if key:
                    data[key] = rec
    return data

def save_csv(path: Path, fields: List[str], rows: List[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        wr = csv.writer(f)
        f.write("# " + ",".join(fields) + "\n")
        for r in rows:
            wr.writerow([r.get(col, "") for col in fields])

def ensure_csv(path: Path, fields: List[str]) -> None:
    if not path.exists():
        save_csv(path, fields, [])

def parse_weekly_text(raw_text: str) -> List[dict]:
    lines = [l.rstrip() for l in raw_text.splitlines()]
    events = []
    current_date = None
    current_venue = None

    for line in lines:
        if not line.strip():
            continue

        canon = _canonical_heading_from_line(line)
        if canon:
            current_date = canon
            current_venue = None
            continue


        if set(line.strip()) == {"*"}:
            continue

        # Explicitly catch "VENUE * TWO SHOWS!" and set venue context
        two_parts = [p.strip() for p in line.split(SPLIT_TOKEN)]
        if len(two_parts) == 2 and two_parts[1].upper().startswith("TWO SHOWS"):
            current_venue = two_parts[0]
            continue

        # Notes-only lines to previous card
        if line.strip().startswith("(") or line.strip().lower().startswith("a songwriters"):
            if events:
                events[-1]["notes"] = (events[-1].get("notes","") + " " + line.strip()).strip()
            continue

        parts = [p.strip() for p in re.split(r"\s*\*\s*", line)]

        # Time-first rows under the same venue
        if len(parts) == 2 and current_venue and re.search(r"\d", parts[0]):
            time = parts[0]
            artist = parts[1]
            events.append({"date": current_date, "venue": current_venue, "artist": artist, "time": time, "notes": ""})
            continue

        # Standard "VENUE * ARTIST * TIME [* extra]"
        if len(parts) >= 3:
            first  = parts[0]
            artist = parts[1]
            time   = parts[2]

            # heuristics: a line is a "venue * artist * time" row if ANY of these are true:
            looks_like_time   = bool(re.search(r"\d", time))  # contains a digit -> likely a time (e.g., 6:00, 6:00 pm, 6–8)
            is_all_caps       = (first.upper() == first)
            has_venue_keyword = bool(VENUE_KEYWORDS_RE.search(first))
            starts_with_num   = bool(re.match(r"^\s*\d", first))  # e.g., "185 King St."

            if is_all_caps or has_venue_keyword or starts_with_num or looks_like_time:
                current_venue = first
                notes = "  *  ".join(parts[3:]).strip() if len(parts) > 3 else ""
                events.append({"date": current_date, "venue": current_venue, "artist": artist, "time": time, "notes": notes})
                continue


        # Supplemental starts-at lines
        if events and any(kw in line.lower() for kw in ("music starts at", "show starts", "starts at")):
            events[-1]["notes"] = (events[-1].get("notes","") + " " + line.strip()).strip()
            continue

        # Lone time update (rare)
        if events and re.search(r"\d", line) and "–" in line and len(line.split()) <= 5:
            events[-1]["time"] = line.strip()
            continue

    return events

def enrich_events(events: List[dict], venues_csv: Path, artists_csv: Path) -> List[dict]:
    ensure_csv(venues_csv, VENUE_FIELDS)
    ensure_csv(artists_csv, ARTIST_FIELDS)
    venues  = load_csv(venues_csv, VENUE_FIELDS)
    artists = load_csv(artists_csv, ARTIST_FIELDS)

    def get_or_create_venue(raw_name: str) -> dict:
        key, row, ok = fuzzy_lookup(raw_name, venues, kind="venue", cutoff=0.92)
        if row: return row
        skel = {
            "text_list_name": raw_name.strip(),
            "html_display_name": raw_name.strip().title(),
            "venue_website": "",
            "google_maps_link": "",
            "town": ""
        }
        venues[key] = skel
        return skel

    def get_or_create_artist(raw_name: str) -> dict:
        display = re.sub(r"^\s*the\s+", "", (raw_name or "").strip(), flags=re.I)
        key, row, ok = fuzzy_lookup(display, artists, kind="artist", cutoff=0.88)
        if row: return row
        skel = {"artist_name": display, "artist_link": "", "artist_image": "", "artist_style": "", "artist_display_name": ""}
        artists[norm(display)] = skel
        return skel

    for ev in events:
        v = get_or_create_venue(ev["venue"])
        a = get_or_create_artist(ev["artist"])
        ev["venue_display"] = v["html_display_name"] or ev["venue"].title()
        ev["venue_link"] = v["venue_website"]
        ev["venue_map"]  = v["google_maps_link"]
        ev["venue_town"] = v.get("town", "").strip()
        ev["artist_link"] = a["artist_link"]
        ev["artist_image"] = a["artist_image"]
        ev["artist_style"] = a["artist_style"]
        ev["artist_display"] = (a.get("artist_display_name") or "").strip() or (ev.get("artist") or "")

        # Image fallback priority: artist → venue → default.
        # Also record where the final image came from so deduping can safely
        # distinguish true artist images from venue/default fallbacks.
        if not ev["artist_image"]:
            venue_image = v.get("venue_image", "").strip()
            if venue_image:
                ev["artist_image"] = venue_image
                ev["_artist_image_source"] = "venue"
            else:
                ev["artist_image"] = "https://www.hendomusic.net/wp-content/media/default.jpg"
                ev["_artist_image_source"] = "default"
        else:
            ev["_artist_image_source"] = "artist"


    save_csv(venues_csv, VENUE_FIELDS, list(venues.values()))
    save_csv(artists_csv, ARTIST_FIELDS, list(artists.values()))
    return events

def _canonical_time_key(time_str: str) -> str:
    """
    Create a normalized representation of an event's time block so that
    cosmetically different strings like "6:30 9:30" and "6:30 pm – 9:30 pm"
    still compare equal when they represent the same range.

    Returns an empty string if we can't confidently parse a start/end pair.
    """
    if not time_str:
        return ""
    s = (time_str or "").strip().lower()
    # Normalize unicode dashes
    s = s.replace("–", "-").replace("—", "-")

    # Find time-like tokens: "5", "5:00", "5pm", "5:00 p.m.", etc.
    time_re = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*(a\.m\.?|p\.m\.?|am|pm)?", re.I)
    matches = list(time_re.finditer(s))
    if len(matches) < 2:
        # Not a clear start/end pair; fall back to a conservative raw string key
        # but only if it obviously looks like a time.
        if any(ch.isdigit() for ch in s):
            s2 = re.sub(r"\s+", " ", s)
            return s2
        return ""

    def norm_suffix(sfx: Optional[str]) -> str:
        if not sfx:
            return ""
        sfx = sfx.lower()
        if "p" in sfx:
            return "p"
        if "a" in sfx:
            return "a"
        return ""

    def extract(m):
        h = int(m.group(1))
        mnt = int(m.group(2) or 0)
        suf = norm_suffix(m.group(3))
        # We don't need absolute 24h time; just a consistent key so that
        # equivalent strings map to the same tuple.
        h = max(1, min(h, 12))
        return h, mnt, suf

    start = extract(matches[0])
    end   = extract(matches[-1])

    (sh, sm, ssuf) = start
    (eh, em, esuf) = end
    return f"{sh:02d}:{sm:02d}{ssuf}-{eh:02d}:{em:02d}{esuf}"


def dedupe_events(events: List[dict]) -> Tuple[List[dict], List[dict]]:
    """
    De-duplicate events *after* enrichment using conservative rules:

    - Same date
    - Same canonical venue (based on website / map URL if available)
    - Same canonical artist (based on artist link or artist-level image)
    - Same normalized time range

    Returns (deduped_events, removed_duplicates).
    """
    seen = {}  # dedupe_key -> index in deduped list
    deduped: List[dict] = []
    dupes: List[dict] = []

    def event_score(ev: dict) -> int:
        """Score an event on richness of metadata to choose which duplicate to keep."""
        score = 0
        if (ev.get("notes") or "").strip():
            score += 1
        if (ev.get("artist_style") or "").strip():
            score += 1
        img = (ev.get("artist_image") or "").strip()
        if img and "default.jpg" not in img:
            score += 1
        if (ev.get("venue_link") or "").strip():
            score += 1
        if (ev.get("venue_map") or "").strip():
            score += 1
        if (ev.get("venue_town") or "").strip():
            score += 1
        return score

    for ev in events:
        date_label = (ev.get("date") or "").strip()
        if not date_label:
            # No date heading implies we can't safely dedupe; keep as-is.
            deduped.append(ev)
            continue

        # Require strong canonical identifiers for both venue and artist so
        # we only collapse duplicates where you've set them up in the CSVs.
        v_link = (ev.get("venue_link") or "").strip().lower()
        v_map  = (ev.get("venue_map") or "").strip().lower()
        a_link = (ev.get("artist_link") or "").strip().lower()
        a_img  = (ev.get("artist_image") or "").strip().lower()
        img_source = ev.get("_artist_image_source")

        has_strong_venue = bool(v_link or v_map)
        # Strong artist identity = link present OR artist-level image (not venue/default).
        has_strong_artist = bool(a_link) or (bool(a_img) and img_source == "artist")

        tkey = _canonical_time_key(ev.get("time") or "")

        if not (has_strong_venue and has_strong_artist and tkey):
            # Not confident enough to dedupe; allow potential duplicates through.
            deduped.append(ev)
            continue

        venue_id  = (v_link, v_map)
        artist_id = (a_link, a_img)
        key = (date_label, venue_id, artist_id, tkey)

        if key not in seen:
            seen[key] = len(deduped)
            deduped.append(ev)
        else:
            idx = seen[key]
            existing = deduped[idx]
            # Decide which event to keep based on metadata richness.
            if event_score(ev) > event_score(existing):
                # New event is better; keep it and mark the old one as a duplicate.
                dupes.append(existing)
                deduped[idx] = ev
            else:
                # Existing is at least as good; drop the new one.
                dupes.append(ev)

    return deduped, dupes


def write_dupes_file(path: Path, dupes: List[dict]) -> None:
    """
    Write removed duplicate events to a simple text file for manual review.
    Each duplicate is represented in a human-friendly, roughly input-like format.
    """
    lines: List[str] = []
    for i, ev in enumerate(dupes, 1):
        date_label = (ev.get("date") or "").strip()
        venue_raw  = (ev.get("venue") or ev.get("venue_display") or "").strip()
        artist_raw = (ev.get("artist") or ev.get("artist_display") or "").strip()
        time_raw   = (ev.get("time") or "").strip()
        notes      = (ev.get("notes") or "").strip()

        lines.append(f"# Duplicate {i}")
        if date_label:
            lines.append(date_label)
        core_parts = [p for p in (venue_raw, artist_raw, time_raw) if p]
        if core_parts:
            lines.append(" * ".join(core_parts))
        if notes:
            lines.append(f"Notes: {notes}")
        lines.append("")  # blank line between entries

    if lines:
        path.write_text("\n".join(lines), encoding="utf-8")


def render_html(events: List[dict], title: str) -> str:

    import re, calendar, json
    from datetime import datetime

    def esc(s): return html.escape(s or "")

    # Guess the year from the page title; fallback to current year.
    m = re.search(r"(19|20)\d{2}", title)
    year_guess = int(m.group(0)) if m else datetime.now().year

    def iso_for_heading(text):
        # Text looks like "Wednesday, September 17"
        m2 = re.search(r"[A-Za-z]+,\s+([A-Za-z]+)\s+(\d{1,2})", text)
        if not m2:
            return ""
        monthname, day = m2.group(1), int(m2.group(2))
        try:
            month = list(calendar.month_name).index(monthname.title())
        except ValueError:
            return ""
        return f"{year_guess:04d}-{month:02d}-{day:02d}"
    
    
    def esc(s): return html.escape(s or "")
    def slug(s):
        s = (s or "").strip().lower()
        s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
        return s or "unknown"

    # order dates as seen; group events
    ordered_dates = list(dict.fromkeys([ev["date"] for ev in events if ev.get("date")]))
    by_date = defaultdict(list)
    for ev in events:
        by_date[ev["date"]].append(ev)

    # unique towns present in THIS week's events (for the checklist)
    towns = []
    seen = set()
    for ev in events:
        t = (ev.get("venue_town") or "").strip()
        key = t.lower()
        if key and key not in seen:
            towns.append(t)
            seen.add(key)
    towns.sort(key=lambda x: x.lower())
    has_unknown = any(not (ev.get("venue_town") or "").strip() for ev in events)

    style_css = """
<style>
/* Base */
.live-music {
  font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif;
  line-height: 1.5;
  --thumb: 108px;    /* thumbnail size (adjust) */
  --gap: 14px;       /* grid gap */
  --sticky-top: 0px; /* bump if your site has a fixed header */
  --date-bg: #0d2b56;
}

/* Headings over the dark-blue hero */
.live-music h1 {
  margin-bottom: .25rem;
  color: #fff !important;
}
.live-music .date {
  position: sticky; top: var(--sticky-top); z-index: 10;
  background: var(--date-bg);
  margin-top: 2rem; padding: .5rem .75rem .6rem;
  border-top: 2px solid rgba(255,255,255,.25) !important;
  box-shadow: 0 1px 0 rgba(255,255,255,.12) inset;
  color: #fff !important;
  white-space: nowrap; overflow: hidden;
  line-height: 1.15; font-weight: 700;
  font-size: clamp(1.1rem, 5.2vw, 2.25rem);
}

/* Filter bar */
.live-music .filterbar { display:flex; gap:.5rem; align-items:center; margin: .5rem 0 1rem; }
.live-music .btn {
  display:inline-flex; align-items:center; gap:.5ch;
  padding:.5rem .75rem; border-radius:8px; font-weight:600; font-size:.95rem;
  background:#eef6ff; color:#175fe6; border:1px solid #cfe1ff;
  text-decoration:none; cursor:pointer;
}
.live-music .btn:hover { background:#e4f0ff; }
.live-music .summary { font-size:.9rem; color:#e6f0ff; margin-left:.25rem; }

/* Cards */
.live-music .card {
  display: grid; grid-template-columns: var(--thumb) 1fr; gap: var(--gap);
  padding: 12px; margin: 10px 0;
  border: 1px solid #dde5f0 !important; border-radius: 12px;
  background: #f6f2fb !important; box-shadow: 0 1px 2px rgba(0,0,0,.03) !important;
  align-items: center;
}
.live-music .thumb {
  width: var(--thumb) !important; height: var(--thumb) !important; object-fit: cover;
  border-radius: 12px; background: #e9eef6 !important;
}

/* Text inside cards */
.live-music .title { font-weight: 600; margin: 0; font-size: 1.05rem; color:#111 !important; }
.live-music .genre { margin: 2px 0 6px; color:#444 !important; font-size: .9em; line-height: 1.3; }
.live-music .genre em { font-style: italic; }
.live-music .venue { margin: 2px 0 6px; color:#111 !important; }
.live-music .venue .town { margin-left:.5rem; color:#666 !important; font-style: italic; }
.live-music .meta  { font-size: .92rem; color:#444 !important; }
.live-music .notes { font-size: .9rem;  color:#555 !important; margin-top: 4px; }

/* Links & badges */
.live-music a { color:#0a7 !important; text-decoration: none; border-bottom: 1px dotted rgba(0,0,0,.2); }
.live-music a:hover { text-decoration: underline; }
.live-music .badgelink {
  border: none; padding: 2px 6px; margin-left: 6px;
  font-size: .8rem; border-radius: 6px;
  background:#eef6ff !important; color:#175fe6 !important;
}

/* Modal */
.live-music .lm-modal[hidden] { display:none; }
.live-music .lm-modal {
  position: fixed; inset: 0; z-index: 1000; background: rgba(0,0,0,.45);
  display:flex; align-items:center; justify-content:center; padding: 20px;
}
.live-music .lm-dialog {
  width:min(680px, 92vw); background:#fff; color:#111; border-radius: 14px;
  box-shadow: 0 10px 30px rgba(0,0,0,.25); padding: 16px 16px 12px; border: 1px solid #e9eef6;
}
.live-music .lm-dialog h3 { margin: 0 0 .5rem; font-size: 1.15rem; }
.live-music .lm-list { display:grid; grid-template-columns: repeat(auto-fill, minmax(180px,1fr)); gap:.35rem .75rem; margin:.5rem 0 1rem; }
.live-music .lm-list label { display:flex; align-items:center; gap:.5rem; font-size:.98rem; }
.live-music .lm-actions { display:flex; gap:.5rem; justify-content:flex-end; }
.live-music .btn.secondary { background:#f3f5f7; color:#111; border-color:#dfe5ea; }
.live-music .btn.danger    { background:#ffecec; color:#8a1c1c; border-color:#ffd1d1; }
@media (max-width: 480px) {
  .live-music { --thumb: 84px; --gap: 12px; }
}
.live-music #lmEarlier { display: none !important; }

</style>
"""

    # Build filter UI (checkboxes generated from `towns`)
    filter_ui = [
        "<div class='filterbar'>",
        "  <button id='lmOpen' class='btn' type='button'>Filter by location</button>",
        "  <span id='lmSummary' class='summary'></span>",
        "</div>",
        "<div class='lm-modal' id='lmModal' hidden>",
        "  <div class='lm-dialog' role='dialog' aria-modal='true' aria-labelledby='lmTitle'>",
        "    <h3 id='lmTitle'>Filter by location</h3>",
        "    <div class='lm-list'>"
    ]
    for t in towns:
        filter_ui.append(
            f"      <label><input type='checkbox' class='lm-town' value='{esc(slug(t))}' data-label='{esc(t)}' checked> {esc(t)}</label>"
        )
    if has_unknown:
        filter_ui.append("      <label><input type='checkbox' class='lm-town' value='unknown' data-label='Other/Unknown' checked> Other/Unknown</label>")
    filter_ui += [
        "    </div>",
        "    <div class='filterbar'>",
        "      <button id='lmOpen' class='btn' type='button'>Filter by location</button>",
        "      <button id='lmEarlier' class='btn secondary' type='button' hidden>Show earlier days</button>",
        "      <span id='lmSummary' class='summary'></span>",
        "    </div>",        
        "    <div class='lm-actions'>",
        "      <button class='btn secondary' type='button' data-act='all'>All</button>",
        "      <button class='btn secondary' type='button' data-act='none'>None</button>",
        "      <button class='btn' type='button' data-act='apply'>Apply</button>",
        "      <button class='btn danger' type='button' data-act='close'>Cancel</button>",
        "    </div>",
        "  </div>",
        "</div>"
    ]

    out = ["<!doctype html><meta charset='utf-8'>", "<div class='live-music'>", style_css]
    out.append(f"<h1>{esc(title)}</h1>")
    out.extend(filter_ui)

    for d in ordered_dates:
        iso = iso_for_heading(d)
        out.append(f"<h2 class='date' data-date='{iso}'>{esc(d)}</h2>")
        for ev in by_date[d]:
            img = ev.get("artist_image") or "data:image/svg+xml;utf8," + html.escape(
                "<svg xmlns='http://www.w3.org/2000/svg' width='72' height='72'>"
                "<rect width='100%' height='100%' fill='%23f3f3f3'/>"
                "<text x='50%' y='50%' dominant-baseline='middle' text-anchor='middle' font-size='12' fill='%23999'>No Image</text>"
                "</svg>"
            )

            town_label = (ev.get("venue_town") or "").strip()
            town_slug  = slug(town_label)

            venue_html = esc(ev.get("venue_display") or ev.get("venue") or "")
            if ev.get("venue_link"):
                venue_html = f"<a href='{html.escape(ev['venue_link'])}' target='_blank' rel='noopener'>{venue_html}</a>"
            # insert town between venue and Map
            if town_label:
                venue_html += f" <span class='town'>({esc(town_label)})</span>"
            if ev.get("venue_map"):
                venue_html += f" <a class='badgelink' href='{html.escape(ev['venue_map'])}' target='_blank' rel='noopener'>Map</a>"

            artist_html = esc(ev.get("artist_display") or ev.get("artist") or "")
            if ev.get("artist_link"):
                artist_html = f"<a href='{html.escape(ev['artist_link'])}' target='_blank' rel='noopener'>{artist_html}</a>"

            time_html  = esc(ev.get("time") or "")
            notes_html = f"<div class='notes'>{esc(ev.get('notes') or '')}</div>" if ev.get("notes") else ""
            style_str  = esc(ev.get("artist_style") or "")
            genre_html = f"<div class='genre'><em>[{style_str}]</em></div>" if style_str else ""

            card = f"""
            <div class="card" data-town="{town_slug}">
              <img class="thumb" src="{img}" alt="">
              <div>
                  <p class="title">{artist_html}</p>
                  {genre_html}
                  <div class="venue">{venue_html}</div>
                  <div class="meta">{time_html}</div>
                  {notes_html}
              </div>
            </div>
            """
            out.append(card)

    # Filtering script (vanilla JS; remembers selection)
    out.append("""
<script>
(function(){
  const modal   = document.getElementById('lmModal');
  const openBtn = document.getElementById('lmOpen');
  const earlierBtn = document.getElementById('lmEarlier');
  const summary = document.getElementById('lmSummary');

  const checks = Array.from(modal.querySelectorAll('.lm-town'));
  const root   = document.querySelector('.live-music');
  const cards  = Array.from(root.querySelectorAll('.card'));
  const dates  = Array.from(root.querySelectorAll('.date'));

  function loadState(){
    try { const raw = localStorage.getItem('lmTownFilter'); return raw ? JSON.parse(raw) : null; }
    catch(e){ return null; }
  }
  function saveState(arr){ localStorage.setItem('lmTownFilter', JSON.stringify(arr)); }

  function applyTownFilter(sel){
    const set = new Set(sel && sel.length ? sel : checks.map(c=>c.value));
    // show/hide cards by town
    cards.forEach(card => {
      const t = card.getAttribute('data-town') || 'unknown';
      card.style.display = set.has(t) ? '' : 'none';
    });
    // hide date headings with zero visible cards under them (pre-past-day trim)
    dates.forEach(h2 => {
      let count = 0, el = h2.nextElementSibling;
      while (el && !el.classList.contains('date')) {
        if (el.classList.contains('card') && el.style.display !== 'none') count++;
        el = el.nextElementSibling;
      }
      h2.dataset.visibleCount = String(count);
      h2.style.display = count ? '' : 'none';
    });

    // summary text
    const labels = checks.filter(c=>c.checked).map(c=>c.getAttribute('data-label') || (c.value==='unknown' ? 'Other/Unknown' : c.value));
    summary.textContent = (labels.length && labels.length < checks.length) ? labels.join(', ') : '';
  }

  function hidePastDays({forceShowAll = false} = {}){
    const today = new Date(); today.setHours(0,0,0,0);
    let anyHidden = false;
    let anyFutureVisible = false;

    dates.forEach(h2 => {
      // If this date has no visible cards from town filter, skip; we already hid it.
      if (h2.style.display === 'none' && h2.dataset.visibleCount === '0') return;

      const iso = h2.getAttribute('data-date');
      const day = iso ? new Date(iso + 'T00:00:00') : null;

      // count visible cards currently under this date
      let count = 0, el = h2.nextElementSibling;
      const blockEls = [];
      while (el && !el.classList.contains('date')) {
        blockEls.push(el);
        if (el.classList.contains('card') && el.style.display !== 'none') count++;
        el = el.nextElementSibling;
      }

      if (!forceShowAll && day && day < today && count > 0) {
        // Hide past block
        h2.style.display = 'none';
        blockEls.forEach(e => e.style.display = 'none');
        anyHidden = true;
      } else {
        // Show today/future (or all when forced)
        if (h2.dataset.visibleCount !== '0') {
          h2.style.display = '';
          blockEls.forEach(e => { if (e.classList.contains('card')) e.style.display = e.style.display || ''; });
        }
        if (day && day >= today && count > 0) anyFutureVisible = true;
      }
    });

    // If everything ended up hidden (e.g., whole week is in the past or town filter left no future days), show all.
    if (!anyFutureVisible) {
      dates.forEach(h2 => {
        // reveal heading if there were any cards under it originally
        if (h2.dataset.visibleCount !== '0') {
          h2.style.display = '';
          let el = h2.nextElementSibling;
          while (el && !el.classList.contains('date')) {
            if (el.classList.contains('card')) el.style.display = '';
            el = el.nextElementSibling;
          }
        }
      });
      anyHidden = false;
    }

    earlierBtn.hidden = !anyHidden;
  }

  // Modal wiring
  function open(){ modal.hidden = false; }
  function close(){ modal.hidden = true; }

  openBtn.addEventListener('click', open);
  modal.addEventListener('click', (e)=>{ if(e.target === modal) close(); });

  modal.querySelector('[data-act="close"]').addEventListener('click', close);
  modal.querySelector('[data-act="all"]').addEventListener('click', ()=>{ checks.forEach(c=>c.checked = true); });
  modal.querySelector('[data-act="none"]').addEventListener('click', ()=>{ checks.forEach(c=>c.checked = false); });
  modal.querySelector('[data-act="apply"]').addEventListener('click', ()=>{
    const sel = checks.filter(c=>c.checked).map(c=>c.value);
    saveState(sel);
    applyTownFilter(sel);
    hidePastDays();     // re-trim past days after town filter changes
    close();
  });

  // Earlier-days button: reveal everything
  earlierBtn.addEventListener('click', ()=>{
    hidePastDays({forceShowAll:true});
    earlierBtn.hidden = true;
  });

  // Init
  const saved = loadState();
  if (saved) checks.forEach(c => c.checked = saved.includes(c.value));
  applyTownFilter(saved);
  hidePastDays();
})();
</script>

""")

    out.append("</div>")
    return "\n".join(out)



def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input_text")
    ap.add_argument("-o", "--output_html")
    ap.add_argument("--venues_csv")
    ap.add_argument("--artists_csv")
    ap.add_argument("--title", default="Local Live Music")
    ap.add_argument(
        "--write-dupes",
        action="store_true",
        dest="write_dupes",
        help="If set, write removed duplicate events to a *_dupes.txt file next to the HTML output.",
    )
    args = ap.parse_args()

    input_path = Path(args.input_text)
    venues_csv  = Path(args.venues_csv)  if args.venues_csv  else input_path.parent / "venues.csv"
    artists_csv = Path(args.artists_csv) if args.artists_csv else input_path.parent / "artists.csv"

    raw = input_path.read_text(encoding="utf-8")
    events = parse_weekly_text(raw)
    events = enrich_events(events, venues_csv, artists_csv)

    # Decide final HTML output path early so we can derive the dupes filename from it.
    output_html = Path(args.output_html) if args.output_html else input_path.with_suffix(".html")

    # De-duplicate after enrichment, using canonical venue/artist info and normalized times.
    deduped_events, dupes = dedupe_events(events)

    if args.write_dupes and dupes:
        dupes_path = output_html.with_name(output_html.stem + "_dupes.txt")
        write_dupes_file(dupes_path, dupes)
        print(f"Wrote dupes file: {dupes_path}")

    output_html.write_text(render_html(deduped_events, args.title), encoding="utf-8")

    print(f"Parsed events (before dedupe): {len(events)}")
    print(f"Events after dedupe: {len(deduped_events)}")
    if dupes:
        print(f"Duplicates removed: {len(dupes)}")
    print(f"Wrote HTML: {output_html}")
    print(f"CSV: {venues_csv} / {artists_csv}")

if __name__ == "__main__":
    main()


if __name__ == "__main__":
    main()
