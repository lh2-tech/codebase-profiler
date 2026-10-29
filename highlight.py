"""Colour coding for the spreadsheet copies of summary.csv (a CSV cannot hold colours).

Three signals, one colour each, used the same way in ``summary.xlsx`` and ``project_compiled.xlsx``:

  duplicate    the repo's duplicate_pct is above DUPLICATE_THRESHOLD_PCT (duplicate_of is filled)
  open source  open_source_loc_pct carries a PR count above 0 (or the repo-level share is above 0)
  vibe coded   vibe_code_signals lists at least one signal, or carries a PR count above 0

``summary.xlsx`` marks the duplicate repos and, individually, the open-source / vibe-coded cells where more than half of
at least one PR is affected. ``project_compiled.xlsx`` marks every repo that is positive for any of the three and
explains the colours in column Z.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

import commit_dedup
import pr_signals

DUPLICATE_COLOR = "FFE0B2"
OPEN_SOURCE_COLOR = "BBDEFB"
VIBE_COLOR = "C8E6C9"
LEGEND_COLUMN = 26  # Z
COMPILED_FILE_NAME = "project_compiled.xlsx"
COMPILED_SHEET = "project compiled"
COMPILED_COLUMNS = [
    "project_name", "subgroup", "repo", "primary_language", "loc", "merged_prs", "total_commits",
    "contributor_count", "duplicate_pct", "duplicate_of", "duplicate_loc", "duplicate_prs",
    "open_source_loc_pct", "vibe_code_signals",
]
# Columns whose text must stay text (they hold "12.5,3,>50%:1" or JSON), the rest become numbers when they look like one.
_TEXT_COLUMNS = {"project_name", "subgroup", "repo", "primary_language", "duplicate_of", "open_source_loc_pct",
                 "vibe_code_signals"}


def _filled(value: Any) -> bool:
    return str(value or "").strip() not in ("", "-", "[]")


def is_duplicate(row: dict[str, str]) -> bool:
    return _filled(row.get("duplicate_of"))


def _repo_level_number(value: Any) -> float:
    head = str(value or "").split(",", 1)[0].strip()
    try:
        return float(head)
    except ValueError:
        return 0.0


def is_open_source(row: dict[str, str]) -> bool:
    counts = pr_signals.read_pr_counts(row.get("open_source_loc_pct"))
    return _repo_level_number(row.get("open_source_loc_pct")) > 0 or bool(counts and counts[0] > 0)


def open_source_majority(row: dict[str, str]) -> bool:
    counts = pr_signals.read_pr_counts(row.get("open_source_loc_pct"))
    return bool(counts and counts[1] > 0)


def is_vibe_coded(row: dict[str, str]) -> bool:
    value = str(row.get("vibe_code_signals") or "")
    counts = pr_signals.read_pr_counts(value)
    if counts and counts[0] > 0:
        return True
    return _filled(pr_signals.strip_pr_counts(value))


def vibe_majority(row: dict[str, str]) -> bool:
    counts = pr_signals.read_pr_counts(row.get("vibe_code_signals"))
    return bool(counts and counts[1] > 0)


def summary_fills(header: list[str], line: list[str]) -> dict[int, str]:
    """Cell fills for one row of the summary sheet: column index -> hex colour.

    A duplicate repo gets its repo cell and the duplicate columns coloured. An open-source or vibe-coded cell is
    coloured on its own, and only when more than half of at least one PR is affected."""
    row = dict(zip(header, line))
    fills: dict[int, str] = {}
    index = {name: i for i, name in enumerate(header)}
    if is_duplicate(row):
        for name in ["repo", *commit_dedup.ALL_FIELDS]:
            if name in index:
                fills[index[name]] = DUPLICATE_COLOR
    if open_source_majority(row) and "open_source_loc_pct" in index:
        fills[index["open_source_loc_pct"]] = OPEN_SOURCE_COLOR
    if vibe_majority(row) and "vibe_code_signals" in index:
        fills[index["vibe_code_signals"]] = VIBE_COLOR
    return fills


def _value(name: str, text: str) -> Any:
    if name in _TEXT_COLUMNS or not text:
        return text
    try:
        number = float(text)
    except ValueError:
        return text
    return int(number) if number.is_integer() and "." not in text else number


def write_project_compiled(summary_csv: Path) -> Path | None:
    """``project_compiled.xlsx`` beside summary.csv: one row per repo, every repo that is positive for duplicate,
    open source or vibe coded coloured per signal, and the colour key in column Z. None when openpyxl is missing.
    Raises on failure: the caller logs it, so a spreadsheet problem cannot abort the run."""
    try:
        import openpyxl
        from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
        from openpyxl.styles import Font, PatternFill
    except ImportError:
        return None

    with summary_csv.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    rows.sort(key=lambda r: ((r.get("project_name") or "").lower(), (r.get("subgroup") or "").lower(),
                             (r.get("repo") or "").lower()))

    def fill(color: str) -> PatternFill:
        return PatternFill(start_color=color, end_color=color, fill_type="solid")

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = COMPILED_SHEET
    sheet.append(COMPILED_COLUMNS)
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    column = {name: i + 1 for i, name in enumerate(COMPILED_COLUMNS)}

    for r, row in enumerate(rows, start=2):
        for name in COMPILED_COLUMNS:
            raw = ILLEGAL_CHARACTERS_RE.sub("", row.get(name) or "")
            cell = sheet.cell(row=r, column=column[name], value=_value(name, raw))
            if isinstance(cell.value, str) and cell.value.startswith(("=", "+", "-", "@")):
                cell.data_type = "s"
        if row.get("error"):
            continue  # a failed repo has no trustworthy figures
        dup, oss, vibe = is_duplicate(row), is_open_source(row), is_vibe_coded(row)
        if dup:
            for name in commit_dedup.ALL_FIELDS:
                if name in column:
                    sheet.cell(row=r, column=column[name]).fill = fill(DUPLICATE_COLOR)
        if oss:
            sheet.cell(row=r, column=column["open_source_loc_pct"]).fill = fill(OPEN_SOURCE_COLOR)
        if vibe:
            sheet.cell(row=r, column=column["vibe_code_signals"]).fill = fill(VIBE_COLOR)
        # The repo cell can carry one colour only: duplicate wins over open source over vibe coded.
        repo_color = DUPLICATE_COLOR if dup else OPEN_SOURCE_COLOR if oss else VIBE_COLOR if vibe else None
        if repo_color:
            sheet.cell(row=r, column=column["repo"]).fill = fill(repo_color)

    threshold = commit_dedup.threshold_pct()
    legend = [
        ("Colour key", None),
        (f"Duplicate: more than {threshold:g}% of the repo's commits exist in another repo", DUPLICATE_COLOR),
        ("Open source: open-source code found (share of LOC, or PRs touching it)", OPEN_SOURCE_COLOR),
        ("Vibe coded: AI-tool signals found (LLM signals, or PRs with an AI signature)", VIBE_COLOR),
        ("The repo name takes one colour only: duplicate, then open source, then vibe coded", None),
    ]
    for offset, (text, color) in enumerate(legend, start=1):
        cell = sheet.cell(row=offset, column=LEGEND_COLUMN, value=text)
        if color:
            cell.fill = fill(color)
        elif offset == 1:
            cell.font = Font(bold=True)
    sheet.column_dimensions["Z"].width = 90
    sheet.freeze_panes = "A2"

    path = summary_csv.with_name(COMPILED_FILE_NAME)
    workbook.save(path)
    return path
