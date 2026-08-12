#!/usr/bin/env python3
"""Artist Match Writer v6

Review only the newest name-only rows at the bottom of an artist CSV. For each
row, choose a fuzzy-matched existing artist and copy its metadata directly into
the pending row without changing the scraped artist name.

RapidFuzz is optional but recommended:
    py -m pip install rapidfuzz
"""

from __future__ import annotations

import csv
import os
import re
import shutil
import sys
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    from rapidfuzz import fuzz
    HAVE_RAPIDFUZZ = True
except ImportError:
    HAVE_RAPIDFUZZ = False


APP_TITLE = "Artist Match Writer"
NAME_FIELD = "# artist_name"
METADATA_FIELDS = [
    "artist_link",
    "artist_image",
    "artist_style",
    "artist_display_name",
]

# Removed only while matching. The actual CSV artist name is never altered.
EVENT_PHRASES = [
    r"\blive\s+music\s+(?:on\s+the\s+patio\s+)?(?:with|w/|featuring|from)\b",
    r"\blive\s+music\b",
    r"\bmusic\s+(?:on\s+the\s+patio|in\s+the\s+taproom|in\s+the\s+garden)\s+(?:with|w/|featuring)?\b",
    r"\bround\s+robin\s+open\s+mic\s+(?:hosted\s+by|with|w/)?\b",
    r"\bopen\s+mic(?:\s+night)?\s+(?:hosted\s+by|with|w/)?\b",
    r"\bjam\s+(?:hosted\s+by|with|w/)?\b",
    r"\bbrunch\s+(?:with|w/|featuring)\b",
    r"\b(?:presented|hosted)\s+by\b",
    r"\bfeaturing\b",
]

ACT_DESCRIPTORS = {
    "the", "band", "trio", "duo", "quartet", "ensemble", "orchestra",
    "project", "group", "friends", "friend", "and", "acoustic", "solo",
}


@dataclass
class ArtistRow:
    index: int
    data: Dict[str, str]

    @property
    def name(self) -> str:
        return (self.data.get(NAME_FIELD) or "").strip()

    def has_any_metadata(self) -> bool:
        return any((self.data.get(field) or "").strip() for field in METADATA_FIELDS)

    def is_name_only(self) -> bool:
        return bool(self.name) and not self.has_any_metadata()


@dataclass
class MatchResult:
    row: ArtistRow
    score: float
    reason: str


def normalize_basic(value: str) -> str:
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.casefold().replace("&", " and ")
    value = re.sub(r"[’'`]+", "", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def strip_event_language(value: str) -> str:
    text = value.casefold().replace("&", " and ")
    for pattern in EVENT_PHRASES:
        text = re.sub(pattern, " ", text, flags=re.IGNORECASE)
    return normalize_basic(text)


def normalize_core(value: str) -> str:
    cleaned = strip_event_language(value)
    tokens = [token for token in cleaned.split() if token not in ACT_DESCRIPTORS]
    return " ".join(tokens) or cleaned


def token_overlap(a: str, b: str) -> float:
    left, right = set(a.split()), set(b.split())
    if not left or not right:
        return 0.0
    return 100.0 * len(left & right) / min(len(left), len(right))


@lru_cache(maxsize=100_000)
def score_names(query: str, candidate: str) -> Tuple[float, str]:
    q_basic = normalize_basic(query)
    c_basic = normalize_basic(candidate)
    q_clean = strip_event_language(query)
    c_clean = strip_event_language(candidate)
    q_core = normalize_core(query)
    c_core = normalize_core(candidate)

    if q_basic and q_basic == c_basic:
        return 100.0, "exact normalized match"
    if q_clean and q_clean == c_clean:
        return 99.0, "same name after event wording"
    if q_core and q_core == c_core:
        return 98.0, "same core artist name"

    if HAVE_RAPIDFUZZ:
        ratios = [
            fuzz.ratio(q_basic, c_basic),
            fuzz.WRatio(q_clean, c_clean),
            fuzz.token_set_ratio(q_clean, c_clean),
            fuzz.token_set_ratio(q_core, c_core),
            fuzz.partial_ratio(q_core, c_core),
        ]
    else:
        def seq(a: str, b: str) -> float:
            return 100.0 * SequenceMatcher(None, a, b).ratio() if a and b else 0.0
        ratios = [
            seq(q_basic, c_basic),
            seq(q_clean, c_clean),
            token_overlap(q_clean, c_clean),
            token_overlap(q_core, c_core),
            100.0 if q_core and c_core and (q_core in c_core or c_core in q_core) else 0.0,
        ]

    overlap = token_overlap(q_core, c_core)
    containment = 0.0
    if q_core and c_core and (q_core in c_core or c_core in q_core):
        containment = 96.0 if min(len(q_core), len(c_core)) >= 4 else 80.0

    score = max(max(ratios), overlap, containment)
    if containment >= score:
        reason = "one core name contains the other"
    elif overlap >= 90:
        reason = "strong word overlap"
    else:
        reason = "fuzzy name similarity"
    return min(100.0, float(score)), reason


class ArtistCSV:
    def __init__(self) -> None:
        self.path: Optional[Path] = None
        self.headers: List[str] = []
        self.rows: List[ArtistRow] = []
        self.pending: List[ArtistRow] = []
        self.candidates: List[ArtistRow] = []
        self.backup_path: Optional[Path] = None

    def load(self, path: Path) -> None:
        with path.open("r", newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames:
                raise ValueError("The CSV file has no header row.")
            headers = list(reader.fieldnames)
            required = [NAME_FIELD, *METADATA_FIELDS]
            missing = [field for field in required if field not in headers]
            if missing:
                raise ValueError("Missing required columns: " + ", ".join(missing))
            rows = [ArtistRow(i, dict(row)) for i, row in enumerate(reader)]

        backup_path = Path(str(path) + ".old")
        if backup_path.exists():
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            previous = Path(str(backup_path) + f".{stamp}")
            counter = 1
            while previous.exists():
                previous = Path(str(backup_path) + f".{stamp}_{counter}")
                counter += 1
            shutil.move(str(backup_path), str(previous))
        shutil.copy2(path, backup_path)

        pending = self._find_bottom_pending(rows)
        pending_indices = {row.index for row in pending}
        candidates = [
            row for row in rows
            if row.index not in pending_indices and row.has_any_metadata()
        ]

        self.path = path
        self.headers = headers
        self.rows = rows
        self.pending = pending
        self.candidates = candidates
        self.backup_path = backup_path

    @staticmethod
    def _find_bottom_pending(rows: List[ArtistRow]) -> List[ArtistRow]:
        found_reversed: List[ArtistRow] = []
        started = False

        for row in reversed(rows):
            # Ignore truly blank trailing records, although DictReader normally
            # omits blank physical lines.
            if not row.name and not row.has_any_metadata():
                if not started:
                    continue
                break

            if row.is_name_only():
                found_reversed.append(row)
                started = True
                continue

            # The first row containing any populated metadata marks the end of
            # the newly appended block.
            break

        return list(reversed(found_reversed))

    def copy_metadata(
        self, target: ArtistRow, source: ArtistRow, display_name: str
    ) -> None:
        for field in METADATA_FIELDS:
            target.data[field] = source.data.get(field, "") or ""
        target.data["artist_display_name"] = display_name
        self.write_atomic()

    def move_row_to_bottom(self, target: ArtistRow) -> None:
        """Move an unchanged row to the physical end of the CSV and save it."""
        try:
            self.rows.remove(target)
        except ValueError as exc:
            raise RuntimeError("The selected row is no longer present in the CSV.") from exc

        self.rows.append(target)
        for index, row in enumerate(self.rows):
            row.index = index
        self.write_atomic()

    def write_atomic(self) -> None:
        if self.path is None:
            raise RuntimeError("No CSV file is loaded.")
        temp_path = Path(str(self.path) + ".tmp")
        try:
            with temp_path.open("w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=self.headers,
                    extrasaction="ignore",
                    lineterminator="\n",
                )
                writer.writeheader()
                for row in self.rows:
                    writer.writerow(row.data)
            os.replace(temp_path, self.path)
        except Exception:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise


class ArtistMatchApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1200x720")
        self.minsize(960, 600)

        self.model = ArtistCSV()
        self.current_target: Optional[ArtistRow] = None
        self.current_matches: List[MatchResult] = []
        self.review_rows: List[ArtistRow] = []

        self.csv_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Select an artist CSV to begin.")
        self.target_var = tk.StringVar(value="No pending row selected")
        self.search_var = tk.StringVar()
        self.detail_vars = {field: tk.StringVar() for field in METADATA_FIELDS}

        self._build_ui()

        supplied = Path(sys.argv[1]) if len(sys.argv) > 1 else None
        if supplied and supplied.exists():
            self.open_csv(supplied)

    def _build_ui(self) -> None:
        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")
        top.columnconfigure(1, weight=1)
        ttk.Label(top, text="Artist CSV:").grid(row=0, column=0, sticky="w", padx=(0, 6))
        ttk.Entry(top, textvariable=self.csv_var, state="readonly").grid(row=0, column=1, sticky="ew")
        ttk.Button(top, text="Open CSV…", command=self.choose_csv).grid(row=0, column=2, padx=(6, 0))

        pane = ttk.Panedwindow(self, orient="horizontal")
        pane.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        left = ttk.Frame(pane, padding=4)
        center = ttk.Frame(pane, padding=4)
        right = ttk.Frame(pane, padding=4)
        pane.add(left, weight=2)
        pane.add(center, weight=4)
        pane.add(right, weight=3)

        ttk.Label(left, text="New rows at bottom", font=("TkDefaultFont", 10, "bold")).pack(anchor="w")
        self.pending_tree = ttk.Treeview(left, columns=("artist",), show="headings", selectmode="browse")
        self.pending_tree.heading("artist", text="Scraped artist_name")
        self.pending_tree.column("artist", width=285, anchor="w")
        left_scroll = ttk.Scrollbar(left, orient="vertical", command=self.pending_tree.yview)
        self.pending_tree.configure(yscrollcommand=left_scroll.set)
        self.pending_tree.pack(side="left", fill="both", expand=True, pady=(5, 0))
        left_scroll.pack(side="right", fill="y", pady=(5, 0))
        self.pending_tree.bind("<<TreeviewSelect>>", self.on_target_selected)

        ttk.Label(center, text="Potential existing matches", font=("TkDefaultFont", 10, "bold")).pack(anchor="w")
        search_frame = ttk.Frame(center)
        search_frame.pack(fill="x", pady=(5, 4))
        ttk.Label(search_frame, text="Search:").pack(side="left")
        self.search_entry = ttk.Entry(search_frame, textvariable=self.search_var)
        self.search_entry.pack(side="left", fill="x", expand=True, padx=(6, 0))
        self.search_entry.bind("<KeyRelease>", self.on_search_changed)
        self.search_entry.bind("<Escape>", self.clear_search)

        self.match_tree = ttk.Treeview(
            center,
            columns=("score", "artist", "style", "display"),
            show="headings",
            selectmode="browse",
        )
        self.match_tree.heading("score", text="Score")
        self.match_tree.heading("artist", text="Existing artist_name")
        self.match_tree.heading("style", text="Style")
        self.match_tree.heading("display", text="Display name")
        self.match_tree.column("score", width=58, anchor="center", stretch=False)
        self.match_tree.column("artist", width=300, anchor="w")
        self.match_tree.column("style", width=150, anchor="w")
        self.match_tree.column("display", width=160, anchor="w")
        match_scroll = ttk.Scrollbar(center, orient="vertical", command=self.match_tree.yview)
        self.match_tree.configure(yscrollcommand=match_scroll.set)
        self.match_tree.pack(side="left", fill="both", expand=True, pady=(5, 0))
        match_scroll.pack(side="right", fill="y", pady=(5, 0))
        self.match_tree.bind("<<TreeviewSelect>>", self.on_match_selected)
        self.match_tree.bind("<Double-1>", lambda _event: self.write_to_row())

        ttk.Label(right, text="Selected new row", font=("TkDefaultFont", 10, "bold")).pack(anchor="w")
        ttk.Label(right, textvariable=self.target_var, wraplength=330, justify="left").pack(anchor="w", pady=(5, 12))

        details = ttk.LabelFrame(right, text="Data that will be copied", padding=8)
        details.pack(fill="x")
        labels = {
            "artist_link": "Link",
            "artist_image": "Image",
            "artist_style": "Style",
            "artist_display_name": "Display name",
        }
        for line, field in enumerate(METADATA_FIELDS):
            ttk.Label(details, text=labels[field] + ":").grid(row=line, column=0, sticky="nw", pady=4)
            if field == "artist_display_name":
                ttk.Entry(
                    details,
                    textvariable=self.detail_vars[field],
                ).grid(row=line, column=1, sticky="ew", padx=(8, 0), pady=4)
                ttk.Button(
                    details,
                    text="Copy from Name",
                    command=self.copy_name_to_display,
                ).grid(row=line + 1, column=1, sticky="w", padx=(8, 0), pady=(0, 4))
            else:
                ttk.Label(
                    details,
                    textvariable=self.detail_vars[field],
                    wraplength=245,
                    justify="left",
                ).grid(row=line, column=1, sticky="nw", padx=(8, 0), pady=4)
        details.columnconfigure(1, weight=1)

        actions = ttk.Frame(right)
        actions.pack(fill="x", pady=(14, 0))
        self.write_button = ttk.Button(actions, text="Write to Row", command=self.write_to_row)
        self.write_button.pack(fill="x")
        self.skip_button = ttk.Button(actions, text="Skip", command=self.skip_current)
        self.skip_button.pack(fill="x", pady=(7, 0))

        note = (
            "Write to Row copies the selected metadata immediately to the CSV. "
            "You may edit Display name first. Skip moves the unchanged row to the bottom. "
            "The scraped artist_name is never changed."
        )
        ttk.Label(right, text=note, wraplength=330, justify="left").pack(anchor="w", pady=(14, 0))

        ttk.Label(self, textvariable=self.status_var, relief="sunken", anchor="w", padding=5).pack(fill="x", side="bottom")

    def choose_csv(self) -> None:
        filename = filedialog.askopenfilename(
            title="Open artist CSV",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if filename:
            self.open_csv(Path(filename))

    def open_csv(self, path: Path) -> None:
        try:
            self.model.load(path)
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"Could not open the CSV:\n\n{exc}")
            return

        self.csv_var.set(str(path))
        self.review_rows = list(self.model.pending)
        self.populate_pending()
        engine = "RapidFuzz" if HAVE_RAPIDFUZZ else "built-in fuzzy matching"
        self.status_var.set(
            f"Found {len(self.review_rows):,} new name-only rows at the bottom. "
            f"Backup: {self.model.backup_path.name if self.model.backup_path else ''}. "
            f"Using {engine}."
        )

    def populate_pending(self) -> None:
        self.pending_tree.delete(*self.pending_tree.get_children())
        for position, row in enumerate(self.review_rows):
            self.pending_tree.insert("", "end", iid=str(position), values=(row.name,))

        self.current_target = None
        self.current_matches = []
        self.search_var.set("")
        self.match_tree.delete(*self.match_tree.get_children())
        self.target_var.set("No pending row selected")
        self.clear_details()

        children = self.pending_tree.get_children()
        if children:
            self.pending_tree.selection_set(children[0])
            self.pending_tree.focus(children[0])
            self.pending_tree.see(children[0])
            # selection_set generates <<TreeviewSelect>>; do not also call the
            # handler manually, which would calculate every fuzzy score twice.

    def on_target_selected(self, _event=None) -> None:
        selection = self.pending_tree.selection()
        if not selection:
            return
        try:
            position = int(selection[0])
            self.current_target = self.review_rows[position]
        except (ValueError, IndexError):
            return
        self.target_var.set(self.current_target.name)

        # A manual search applies only to the row for which it was entered.
        # Selecting another pending row, including the automatic selection after
        # Write to Row or Skip, restores that row's fuzzy suggestions.
        self.search_var.set("")
        self.refresh_matches()

    def on_search_changed(self, _event=None) -> None:
        if self.current_target is not None:
            self.refresh_matches()

    def clear_search(self, _event=None) -> str:
        self.search_var.set("")
        if self.current_target is not None:
            self.refresh_matches()
        return "break"

    def refresh_matches(self) -> None:
        self.current_matches = []
        self.match_tree.delete(*self.match_tree.get_children())
        self.clear_details()
        if self.current_target is None:
            return

        manual_query = self.search_var.get().strip()
        results: List[MatchResult] = []
        for candidate in self.model.candidates:
            if manual_query:
                searchable = " ".join([
                    candidate.name,
                    candidate.data.get("artist_display_name", "") or "",
                ])
                query_tokens = normalize_basic(manual_query).split()
                haystack = normalize_basic(searchable)
                if not query_tokens or not all(token in haystack for token in query_tokens):
                    continue
                score, reason = score_names(manual_query, searchable)
                reason = "manual search"
            else:
                score, reason = score_names(self.current_target.name, candidate.name)
            results.append(MatchResult(candidate, score, reason))

        results.sort(
            key=lambda result: (
                result.score,
                sum(bool((result.row.data.get(field) or "").strip()) for field in METADATA_FIELDS),
            ),
            reverse=True,
        )
        self.current_matches = results[:100] if manual_query else results[:25]

        for position, result in enumerate(self.current_matches):
            self.match_tree.insert(
                "",
                "end",
                iid=str(position),
                values=(
                    f"{result.score:.0f}",
                    result.row.name,
                    result.row.data.get("artist_style", ""),
                    result.row.data.get("artist_display_name", ""),
                ),
            )

        children = self.match_tree.get_children()
        if children:
            self.match_tree.selection_set(children[0])
            self.match_tree.focus(children[0])
            self.match_tree.see(children[0])
            self.on_match_selected()

    def on_match_selected(self, _event=None) -> None:
        source = self.selected_match()
        if source is None:
            self.clear_details()
            return
        for field in METADATA_FIELDS:
            self.detail_vars[field].set(source.data.get(field, "") or "")

    def copy_name_to_display(self) -> None:
        if self.current_target is None:
            return
        self.detail_vars["artist_display_name"].set(self.current_target.name)

    def selected_match(self) -> Optional[ArtistRow]:
        selection = self.match_tree.selection()
        if not selection:
            return None
        try:
            return self.current_matches[int(selection[0])].row
        except (ValueError, IndexError):
            return None

    def clear_details(self) -> None:
        for variable in self.detail_vars.values():
            variable.set("")

    def write_to_row(self) -> None:
        target = self.current_target
        source = self.selected_match()
        if target is None:
            messagebox.showinfo(APP_TITLE, "Select a new row first.")
            return
        if source is None:
            messagebox.showinfo(APP_TITLE, "Select an existing artist match first.")
            return

        try:
            self.model.copy_metadata(
                target, source, self.detail_vars["artist_display_name"].get()
            )
        except Exception as exc:
            messagebox.showerror(
                APP_TITLE,
                f"The CSV could not be updated. The .old backup was not changed.\n\n{exc}",
            )
            return

        self.remove_current(
            f"Wrote metadata from {source.name!r} to {target.name!r}."
        )

    def skip_current(self) -> None:
        target = self.current_target
        if target is None:
            return

        skipped_name = target.name
        try:
            self.model.move_row_to_bottom(target)
        except Exception as exc:
            messagebox.showerror(
                APP_TITLE,
                f"The row could not be moved. The .old backup was not changed.\n\n{exc}",
            )
            return

        self.remove_current(
            f"Moved skipped row {skipped_name!r} to the bottom of the CSV."
        )

    def remove_current(self, status: str) -> None:
        target = self.current_target
        if target is None:
            return

        selection = self.pending_tree.selection()
        selected_iid = selection[0] if selection else None
        try:
            old_position = self.review_rows.index(target)
        except ValueError:
            old_position = 0

        self.review_rows.remove(target)
        self.current_target = None
        self.current_matches = []

        # Remove only the completed item instead of rebuilding the entire left
        # tree. Rebuilding also caused redundant selection events and matching.
        if selected_iid and self.pending_tree.exists(selected_iid):
            self.pending_tree.delete(selected_iid)

        # Tree item IDs originally represented list positions. Renumber the
        # remaining items cheaply so on_target_selected still maps correctly.
        remaining_names = [row.name for row in self.review_rows]
        self.pending_tree.delete(*self.pending_tree.get_children())
        for position, name in enumerate(remaining_names):
            self.pending_tree.insert("", "end", iid=str(position), values=(name,))

        children = self.pending_tree.get_children()
        if children:
            next_position = min(old_position, len(children) - 1)
            item = children[next_position]
            self.pending_tree.selection_set(item)
            self.pending_tree.focus(item)
            self.pending_tree.see(item)
            # Let the virtual selection event perform one match refresh.
            self.status_var.set(f"{status} {len(self.review_rows):,} rows remain.")
        else:
            self.match_tree.delete(*self.match_tree.get_children())
            self.target_var.set("No pending row selected")
            self.clear_details()
            self.status_var.set(status + " Review complete.")
            messagebox.showinfo(APP_TITLE, "All new rows have been reviewed.")


def main() -> None:
    app = ArtistMatchApp()
    app.mainloop()


if __name__ == "__main__":
    main()
