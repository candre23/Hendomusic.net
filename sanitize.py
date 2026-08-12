#!/usr/bin/env python3
"""
sanitize_csv.py
---------------
Replace Windows-1252 / “smart” punctuation and other non-ASCII glyphs in CSVs
with simple keyboard characters, and write clean UTF-8 output.

Usage:
  python sanitize_csv.py artists.csv venues.csv
  python sanitize_csv.py path/to/dir --glob "*.csv"

Options:
  --glob PATTERN   Only process files matching PATTERN inside a directory.
  --dry-run        Show what would be changed, but do not write files.
  --no-backup      Do not write .bak backups.
"""

import argparse, sys, os, re, unicodedata
from pathlib import Path

# Common Windows-1252 / smart punctuation -> ASCII replacements
SMART_MAP = {
    "\u00A0": " ",   # NBSP -> space
    "\u00AD": "-",   # soft hyphen -> hyphen
    "\u2010": "-",   # hyphen
    "\u2011": "-",   # non-breaking hyphen
    "\u2012": "-",   # figure dash
    "\u2013": "-",   # en dash
    "\u2014": "-",   # em dash
    "\u2015": "-",   # horizontal bar
    "\u2026": "...", # ellipsis
    "\u2018": "'",   # left single quotation mark
    "\u2019": "'",   # right single quotation mark / apostrophe
    "\u201A": "'",   # single low-9 quotation mark
    "\u201B": "'",   # single high-reversed-9 quotation mark
    "\u201C": '"',   # left double quotation mark
    "\u201D": '"',   # right double quotation mark
    "\u201E": '"',   # double low-9 quotation mark
    "\u2032": "'",   # prime
    "\u2033": '"',   # double prime
    "\u2212": "-",   # minus sign
    "\u2022": "-",   # bullet
    "\u00B7": "-",   # middle dot -> hyphen
    "\u00AB": '"',   # «
    "\u00BB": '"',   # »
    "\u00B4": "'",   # acute accent
    "\u02BC": "'",   # modifier letter apostrophe
}

# Characters we want to drop entirely
DROP_CHARS = {
    "\u200B",  # zero-width space
    "\u200C",  # zero-width non-joiner
    "\u200D",  # zero-width joiner
    "\uFEFF",  # BOM
}

def decode_bytes(data: bytes) -> str:
    """Try decoding with cp1252 first (common for Windows CSV), then UTF-8 BOM, then UTF-8."""
    for enc in ("cp1252", "utf-8-sig", "utf-8"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    # Last resort: replace errors to ensure we can continue
    return data.decode("utf-8", errors="replace")

def smart_replace(text: str) -> str:
    # First apply targeted replacements
    out = []
    for ch in text:
        if ch in DROP_CHARS:
            continue
        out.append(SMART_MAP.get(ch, ch))
    text = "".join(out)

    # Then normalize accents (e.g., café -> cafe) into ASCII
    # Keep symbols like ©®™, etc., by dropping their accents/marks
    norm = unicodedata.normalize("NFKD", text)
    # Encode to ASCII, dropping non-ASCII; then back to str
    ascii_text = norm.encode("ascii", "ignore").decode("ascii")
    # Collapse any multiple spaces created by drops
    ascii_text = re.sub(r"[ \t]+", " ", ascii_text)
    return ascii_text

def process_file(path: Path, dry_run: bool=False, backup: bool=True) -> tuple[int,int]:
    raw = path.read_bytes()
    before_text = decode_bytes(raw)
    after_text  = smart_replace(before_text)

    # Count changes
    changes = sum(1 for a, b in zip(before_text, after_text) if a != b) + abs(len(before_text) - len(after_text))
    if not dry_run and changes > 0:
        if backup:
            path.with_suffix(path.suffix + ".bak").write_bytes(raw)
        path.write_text(after_text, encoding="utf-8", newline="")
    return (len(before_text), changes)

def main(argv=None):
    ap = argparse.ArgumentParser(description="Sanitize CSV files to plain-ASCII UTF-8.")
    ap.add_argument("paths", nargs="+", help="CSV file(s) or directory/ies to process")
    ap.add_argument("--glob", default=None, help="When a path is a directory, only files matching this glob are processed (e.g. '*.csv')")
    ap.add_argument("--dry-run", action="store_true", help="Show summary but do not write files")
    ap.add_argument("--no-backup", action="store_true", help="Do not write .bak backups")
    args = ap.parse_args(argv)

    total_files = 0
    total_changes = 0

    for p in args.paths:
        path = Path(p)
        if path.is_dir():
            patt = args.glob or "*.csv"
            files = sorted(path.glob(patt))
        else:
            files = [path]

        for f in files:
            if not f.exists() or not f.is_file():
                print(f"[skip] {f} (not a file)")
                continue
            total_files += 1
            size, changes = process_file(f, dry_run=args.dry_run, backup=not args.no_backup)
            total_changes += changes
            status = "DRY" if args.dry_run else "FIX"
            print(f"[{status}] {f}  size={size}  changes={changes}")

    if total_files == 0:
        print("No files processed.")
    else:
        print(f"Done. Files: {total_files}, total changes: {total_changes}")

if __name__ == "__main__":
    main()
