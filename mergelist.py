#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mergelist.py
------------
Merge multiple plaintext live music lists (in livemusic.txt format) into a single
chronological file, while sanitizing text and deduplicating events that are the
same artist/time on the same day (venue-agnostic), with a configurable time tolerance.

Now also:
- Sorts events within each day by start time (single times treated as start).
- Writes non-standard/unparsable event lines to <OUTPUT>.manual_fixes.txt for review.

USAGE
-----
  python mergelist.py OUTPUT.txt --dir PATH/TO/SOURCE/TXT --start YYYY-MM-DD --end YYYY-MM-DD [--minutes 15] [--verbose]
"""

import sys, os, re, glob, unicodedata
from datetime import datetime, date
from typing import Optional, Tuple, List, Dict

# ---------------------- Helpers: Normalization ----------------------

def sanitize_ascii(s: str) -> str:
    """Convert to ASCII-friendly string (strip diacritics, normalize whitespace/dashes/quotes)."""
    if s is None:
        return ""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = s.replace("—", "-").replace("–", "-")
    # normalize quotes
    s = s.replace("“", '"').replace("”", '"').replace("’", "'").replace("‘", "'")
    s = re.sub(r"\s+", " ", s).strip()
    return s

def is_strict_event_shape(line: str) -> bool:
    """True iff line looks like 'VENUE * ARTIST * TIMES' (3+ fields),
    where TIMES contains a time OR is the literal '??'."""
    raw = sanitize_ascii(line)
    parts = [p.strip() for p in re.split(r"\s*\*\s*", raw)]
    if len(parts) < 3:
        return False
    time_field = parts[2]
    return bool(re.search(r"\d", time_field)) or time_field.strip() == "??"

def _manual_key(text: str) -> str:
    """Normalization used to de-dup manual-review blocks across files."""
    t = sanitize_ascii(text).lower()
    t = re.sub(r'\s+', ' ', t).strip()
    return t


# Common prefixes that are not part of the artist's name
PREFIX_PATTERNS = [
    r"^live music\s*:\s*",
    r"^open mic night\s*:\s*",
    r"^patio\s*:\s*",
    r"^saturday night live\s*\|\s*",
    r"^hazy music series\s*\|\s*",
    r"^oktoberfest.*?:\s*",
    r"^oktemberfest.*?:\s*",
    r"^block party ft\.\s*",
    r"^round robin open mic hosted by .*?:\s*",
    r"^two shows!\s*",
    r"^anniversary weekend w/\s*",
    r"^fall festival w/\s*",
    r"^historic .*? hosts\s*",
    r"^bluegrass brunch.*?:\s*",
    r"^live music\s*-\s*",
    r"^live music\s*\|\s*",
    r"^music starts at\s*\d+:\d+\s*(?:am|pm)?\s*-\s*",
    r"^sunday jazz jam\s*[:\-]\s*",
]

PREFIX_RE = re.compile("|".join(PREFIX_PATTERNS), re.IGNORECASE)

def normalize_artist(artist: str) -> str:
    a = sanitize_ascii(artist)
    if '|' in a:
        parts = [p.strip() for p in a.split('|')]
        if len(parts) > 1:
            a = parts[-1]
    a = PREFIX_RE.sub("", a)
    a = re.sub(r'["]', "", a)
    a = re.sub(r"\s+\(.*?\)$", "", a)
    a = re.sub(r"\s+", " ", a).strip()
    return a

def artist_key(a: str) -> str:
    a = a.lower()
    a = re.sub(r"&", "and", a)
    a = re.sub(r"[^a-z0-9]+", " ", a)
    return re.sub(r"\s+", " ", a).strip()

# ---------------------- Time Parsing ----------------------

TIME_TOKEN_RE = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", re.IGNORECASE)

def to_minutes(hour: int, minute: Optional[str], ampm: Optional[str]) -> int:
    hour = int(hour)
    minute = int(minute) if minute is not None else 0
    ampm = (ampm or "").lower()
    if ampm == "am":
        if hour == 12:
            hour = 0
    else:
        # default to PM if not specified
        if hour < 12:
            hour += 12
    return hour * 60 + minute

def parse_times(timestr: str) -> Tuple[Optional[int], Optional[int]]:
    """Return (start_min, end_min). If only one time is present, end_min is None."""
    if not timestr:
        return None, None
    s = sanitize_ascii(timestr).lower()
    s = s.replace(" to ", " - ").replace("—","-").replace("–","-")
    tokens = list(TIME_TOKEN_RE.finditer(s))
    if len(tokens) >= 2:
        t1, t2 = tokens[0], tokens[1]
        start = to_minutes(t1.group(1), t1.group(2), t1.group(3))
        end   = to_minutes(t2.group(1), t2.group(2), t2.group(3))
        if end <= start:
            end += 12 * 60  # same-evening wrap
        return start, end
    if len(tokens) == 1:
        t1 = tokens[0]
        start = to_minutes(t1.group(1), t1.group(2), t1.group(3))
        return start, None
    return None, None

# ---------------------- Multi-show expansion ----------------------

def looks_like_full_event_line(line: str) -> bool:
    return line.count('*') >= 2 and 'two show' not in line.lower()

def is_multishow_header(line: str) -> bool:
    if '*' not in line:
        return False
    parts = [p.strip() for p in line.split('*')]
    if len(parts) < 2:
        return False
    return 'two show' in parts[1].lower()

def extract_venue_from_header(line: str) -> str:
    return line.split('*', 1)[0].strip()

def expand_multishow_blocks(lines: List[str]) -> List[str]:
    out: List[str] = []
    i = 0
    while i < len(lines):
        line = sanitize_ascii(lines[i]).strip()
        if is_multishow_header(line):
            venue = extract_venue_from_header(line)
            i += 1
            while i < len(lines):
                nxt = sanitize_ascii(lines[i]).strip()
                if not nxt or (nxt.startswith('[') and nxt.endswith(']')):
                    i += 1
                    continue
                if looks_like_full_event_line(nxt) or is_multishow_header(nxt):
                    break
                if '*' in nxt:
                    left, right = [s.strip() for s in nxt.split('*', 1)]
                    s1, e1 = parse_times(left)
                    if s1 is not None:
                        time_part = left.replace('—','-').replace('–','-')
                        act = normalize_artist(right)
                        out.append(f"{venue}  *  {act}  *  {time_part}")
                        i += 1
                        continue
                    s2, e2 = parse_times(right)
                    if s2 is not None:
                        time_part = right.replace('—','-').replace('–','-')
                        act = normalize_artist(left)
                        out.append(f"{venue}  *  {act}  *  {time_part}")
                        i += 1
                        continue
                break
            continue
        out.append(line)
        i += 1
    return out

# ---------------------- Date Header Parsing (3.15-safe) ----------------------

DATE_CANDIDATES = [
    "%a, %b %d, %Y",
    "%a, %b %d",
    "%A, %B %d, %Y",
    "%A, %B %d",
    "%a %b %d, %Y",
    "%a %b %d",
    "%b %d, %Y",
    "%b %d",
    "%B %d, %Y",
    "%B %d",
]

def parse_date_header(s: str, year_hint: int, prev_date: Optional[date] = None) -> Optional[date]:
    """
    Parse a header like 'Sat, Oct 4' or 'THURSDAY, JANUARY 1' into a datetime.date.

    - Always inject an explicit year (Python 3.15-safe).
    - If the header omits a year AND it appears to roll over a year boundary
      (e.g., previous date is in Dec and this parses as Jan), bump the year.
    """
    t = sanitize_ascii(s)

    for fmt in DATE_CANDIDATES:
        try:
            inferred = ("%Y" not in fmt)
            if inferred:
                t_with_year = f"{t}, {year_hint}"
                fmt_with_year = fmt + ", %Y"
                dt = datetime.strptime(t_with_year, fmt_with_year).date()

                # Year-rollover correction:
                # If we had a previous parsed date and the new date appears "earlier",
                # and the months look like Dec -> Jan, bump the year.
                if prev_date is not None:
                    if (dt < prev_date) and (prev_date.month == 12) and (dt.month == 1):
                        dt = date(dt.year + 1, dt.month, dt.day)

                return dt
            else:
                return datetime.strptime(t, fmt).date()
        except ValueError:
            continue

    # Fallback like "Oct 4"
    m = re.search(r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2})", t, re.IGNORECASE)
    if m:
        month_str, day_str = m.group(1), m.group(2)
        try:
            dt = datetime.strptime(f"{month_str} {int(day_str)} {year_hint}", "%b %d %Y").date()
            if prev_date is not None:
                if (dt < prev_date) and (prev_date.month == 12) and (dt.month == 1):
                    dt = date(dt.year + 1, dt.month, dt.day)
            return dt
        except Exception:
            pass

    return None


def pretty_date(d: date) -> str:
    return d.strftime("%a, %b ") + str(d.day)

# ---------------------- Venue Aliases ----------------------

VENUE_ALIASES = {
    "bold rock": "Bold Rock Hard Cider",
    "bold rock hard cider": "Bold Rock Hard Cider",
    "bold rock hard cider - mills river": "Bold Rock Hard Cider",
    "bold rock hard cider mills river": "Bold Rock Hard Cider",
}

def canonical_venue(v: str) -> str:
    v2 = sanitize_ascii(v).lower().strip()
    v2 = re.sub(r"\s+", " ", v2)
    return VENUE_ALIASES.get(v2, sanitize_ascii(v).strip())

# ---------------------- Event Parsing ----------------------

def parse_event_line(line: str) -> Tuple[str, str, Optional[int], Optional[int]]:
    """
    Strict parser: only accept 'VENUE * ARTIST * TIMES' rows.
    Returns (venue, artist, start_min, end_min).

    Special case:
      - If TIMES == '??', treat as an unknown start that should be
        sorted to the beginning of the day: start_min = -1, end_min = None.
    """
    raw = sanitize_ascii(line)
    parts = [p.strip() for p in re.split(r"\s*\*\s*", raw)]
    if len(parts) < 3:
        return "", "", None, None

    venue, artist, timestr = parts[0], parts[1], parts[2]

    # Accept explicit unknown time marker '??'
    if timestr.strip() == "??":
        return canonical_venue(venue), normalize_artist(artist), -1, None

    # Must contain at least one digit for normal times
    if not re.search(r"\d", timestr):
        return "", "", None, None

    s, e = parse_times(timestr)
    if s is None:
        return "", "", None, None

    return canonical_venue(venue), normalize_artist(artist), s, e



# ---------------------- Deduplication ----------------------

def is_dup(a_key: str, s: Optional[int], e: Optional[int],
           existing: List[dict], minutes_tol: int) -> Optional[int]:
    if not a_key or s is None:
        return None
    for idx, ev in enumerate(existing):
        if ev["artist_key"] != a_key:
            continue
        if ev["start"] is None:
            continue
        if abs(ev["start"] - s) <= minutes_tol:
            if e is not None and ev["end"] is not None:
                if abs(ev["end"] - e) <= minutes_tol:
                    return idx
            else:
                return idx
    return None

# ---------------------- Main Merge Logic ----------------------

def load_files(src_dir: str) -> List[str]:
    files = []
    for pat in ("*.txt", "*.TXT"):
        files.extend(glob.glob(os.path.join(src_dir, pat)))

    # Windows is case-insensitive, so *.txt and *.TXT can return the same files.
    # De-dup by normalized absolute path.
    uniq = {}
    for f in files:
        uniq[os.path.normcase(os.path.abspath(f))] = f

    return sorted(uniq.values())


def merge_files(files: List[str], start_d: date, end_d: date, minutes_tol: int, verbose: bool=False) -> Tuple[List[str], List[str]]:
    day_map: Dict[date, List[dict]] = {}
    manual_by_day: Dict[date, List[str]] = {}
    manual_seen_by_day: Dict[date, set] = {}

    for fp in files:
        # IMPORTANT: reset these per file so one file's Jan rollover doesn't
        # make the next file's Dec headers look like Dec of the next year.
        year_hint = start_d.year
        last_header_date: Optional[date] = None
        current_date: Optional[date] = None

        if verbose:
            print(f"[READ] {fp}")

        try:
            with open(fp, "r", encoding="utf-8") as f:
                raw_lines = [ln.rstrip("\n") for ln in f]
        except Exception:
            with open(fp, "r", encoding="latin-1") as f:
                raw_lines = [ln.rstrip("\n") for ln in f]

        lines = [sanitize_ascii(ln) for ln in raw_lines]
        lines = expand_multishow_blocks(lines)

        i = 0
        while i < len(lines):
            ln = lines[i]

            if ln.strip() == "*****":
                current_date = None
                i += 1
                continue

            if "*" not in ln:
                d = parse_date_header(ln.strip(), year_hint, prev_date=last_header_date)
                if d:
                    current_date = d
                    last_header_date = d
                    year_hint = d.year
                i += 1
                continue

            # ... rest of your existing loop logic unchanged ...


            # event-like line
            if current_date is None:
                if verbose: print(f"[skip] Orphan event (no date): {ln}")
                i += 1
                continue
            if not (start_d <= current_date <= end_d):
                i += 1
                continue

            # STRICT SHAPE CHECK: must be 'VENUE * ARTIST * TIMES'
            if not is_strict_event_shape(ln):
                # capture this line + following continuation lines (notes/parentheticals)
                block = [ln]
                j = i + 1
                while j < len(lines):
                    nxt = lines[j]
                    if nxt.strip() == "*****":
                        break
                    if is_strict_event_shape(nxt):
                        break
                    if parse_date_header(nxt.strip(), year_hint, prev_date=last_header_date) is not None:
                        break


                    if nxt.strip() and (nxt.strip().startswith("[") or nxt.strip().startswith("(")):
                        block.append(nxt); j += 1; continue
                    if "*" not in nxt:
                        block.append(nxt); j += 1; continue
                    break

                block_text = "\n".join(block).strip()
                key = _manual_key(block_text)
                manual_seen_by_day.setdefault(current_date, set())
                if key not in manual_seen_by_day[current_date]:
                    manual_by_day.setdefault(current_date, []).append(block_text)
                    manual_seen_by_day[current_date].add(key)
                    if verbose: print(f"[manual] {current_date} :: {block[0]}")
                i = j
                continue

            # Parse strict row
            venue, artist, smin, emin = parse_event_line(ln)
            if smin is None:
                # Defensive: treat as manual if strict shape somehow yielded no start
                block_text = ln.strip()
                key = _manual_key(block_text)
                manual_seen_by_day.setdefault(current_date, set())
                if key not in manual_seen_by_day[current_date]:
                    manual_by_day.setdefault(current_date, []).append(block_text)
                    manual_seen_by_day[current_date].add(key)
                    if verbose: print(f"[manual] {current_date} :: {ln}")
                i += 1
                continue

            akey = artist_key(artist)
            rec = {
                "raw": sanitize_ascii(ln),
                "venue": venue,
                "artist": artist,
                "artist_key": akey,
                "start": smin,
                "end": emin,
                "has_venue": bool(venue.strip()),
            }
            day_map.setdefault(current_date, [])
            dup_idx = is_dup(akey, smin, emin, day_map[current_date], minutes_tol)
            if dup_idx is None:
                day_map[current_date].append(rec)
            else:
                if (not day_map[current_date][dup_idx]["has_venue"]) and rec["has_venue"]:
                    day_map[current_date][dup_idx] = rec
                elif day_map[current_date][dup_idx]["end"] is None and rec["end"] is not None:
                    day_map[current_date][dup_idx] = rec
            i += 1

    # Emit merged output chronologically
    out_lines: List[str] = []
    for d in sorted(day_map.keys()):
        out_lines.append("*****")
        out_lines.append(pretty_date(d))
        sorted_events = sorted(
            day_map[d],
            key=lambda ev: (ev["start"] is None, ev["start"] if ev["start"] is not None else 10**6, ev["artist_key"])
        )
        for ev in sorted_events:
            out_lines.append(ev["raw"])

    # Manual review file contents (already de-duplicated)
    manual_lines: List[str] = []
    for d in sorted(manual_by_day.keys()):
        manual_lines.append("*****")
        manual_lines.append(pretty_date(d))
        manual_lines.extend(manual_by_day[d])

    return out_lines, manual_lines


def main(argv=None):
    import argparse
    p = argparse.ArgumentParser(
        description="Merge event txt files with dedupe, sanitization, chronological sort, and manual oddball capture."
    )
    p.add_argument("output", help="Output txt file path")
    p.add_argument("--dir", required=True, help="Directory containing source txt files")
    p.add_argument("--start", required=True, help="Start date YYYY-MM-DD")
    p.add_argument("--end", required=True, help="End date YYYY-MM-DD")
    p.add_argument("--minutes", type=int, default=15, help="Time tolerance in minutes for dup matching (default 15)")
    p.add_argument("--verbose", action="store_true", help="Verbose logs")
    args = p.parse_args(argv)

    start_d = datetime.strptime(args.start, "%Y-%m-%d").date()
    end_d = datetime.strptime(args.end, "%Y-%m-%d").date()
    if end_d < start_d:
        raise SystemExit("ERROR: --end must be on/after --start")

    files = load_files(args.dir)
    if args.verbose:
        print(f"[info] {len(files)} file(s) found in {args.dir}")
    if not files:
        raise SystemExit("No input files found.")

    out_lines, manual_lines = merge_files(files, start_d, end_d, args.minutes, verbose=args.verbose)

    # Write main output
    with open(args.output, "w", encoding="utf-8") as f:
        f.write("\n".join(out_lines))

    # Write manual review file (only if there are oddballs)
    manual_path = args.output + ".manual_fixes.txt"
    if manual_lines:
        with open(manual_path, "w", encoding="utf-8") as f:
            f.write("\n".join(manual_lines))
        if args.verbose:
            print(f"[NOTE] Wrote {len(manual_lines)} manual-review lines -> {manual_path}")
    elif args.verbose:
        print("[NOTE] No oddball lines found; no manual file written.")

    if args.verbose:
        print(f"[OK] wrote {len(out_lines)} lines -> {args.output}")


if __name__ == "__main__":
    main()
