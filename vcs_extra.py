"""Metrics that matter for non-Git history (Mercurial, Perforce) but that the Git-shaped summary hides.

The summary columns stay identical across VCSs so sheets remain comparable. These extras go in
``git/<repo>/vcs_extra.json`` and, per run, ``vcs_extra_metrics.csv``. They answer the questions the
Git-shaped columns cannot: how big is a typical change (a Perforce changelist is not a Git commit),
how much of the content is not code (binary assets), how concentrated the authorship is, whether the
project is alive, and whether commit messages carry any information.

Everything is derived from metadata and file sizes; no file contents leave the machine. The binary
check reads at most the first 8 KB of each file locally.
"""
from __future__ import annotations

import csv
import json
import os
import re
import statistics
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

SKIP_DIRS = {".hg", ".git", ".svn", ".p4config"}
MEGA_CHANGE_LINES = 1000
LARGE_FILE_BYTES = 10 * 1024 * 1024
TOP_N = 10

# A message that says nothing: empty, a tool default, or a single throw-away word.
_GENERIC_SUBJECT = re.compile(
    r"^\s*(?:|<?enter description here>?|\*\*\* ?(?:empty|no description)[^\n]*|no message|"
    r"none|<none>|update|updates|fix|fixes|wip|test|tmp|temp|misc|changes?|\.|-+)\s*$",
    re.I,
)
# Evidence that a change went through review. Perforce Swarm and Phabricator/Mozilla/Gerrit tags.
_REVIEW_MARK = re.compile(
    r"#review-?\d*|\breview(?:ed)?[ :#-]+\d+|\bswarm\b|differential revision|\br=[\w,]+|"
    r"\breviewed[- ]by\b|\bpull request\b|\bcode review\b|\breviewboard\b",
    re.I,
)


def _pct(part: float, whole: float) -> float | None:
    return round(100 * part / whole, 1) if whole else None


def _quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def _parse(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------- the working tree


def _is_binary(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            return b"\x00" in fh.read(8000)
    except OSError:
        return False


def tree_inventory(tree: Path) -> dict[str, Any]:
    """Size and content-type picture of a checked-out tree: how much of it is code-like text at all."""
    files = binary_files = text_files = zero = large = 0
    total = binary_bytes = 0
    by_ext_bytes: Counter[str] = Counter()
    by_ext_files: Counter[str] = Counter()
    largest: list[tuple[int, str]] = []
    for root, dirs, names in os.walk(tree):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in names:
            p = Path(root) / name
            try:
                if p.is_symlink():
                    continue
                size = p.stat().st_size
            except OSError:
                continue
            files += 1
            total += size
            ext = p.suffix.lower() or "(none)"
            by_ext_bytes[ext] += size
            by_ext_files[ext] += 1
            if size == 0:
                zero += 1
            elif _is_binary(p):
                binary_files += 1
                binary_bytes += size
            else:
                text_files += 1
            if size >= LARGE_FILE_BYTES:
                large += 1
            largest.append((size, p.relative_to(tree).as_posix()))
            if len(largest) > 50:
                largest = sorted(largest, reverse=True)[:TOP_N]
    largest = sorted(largest, reverse=True)[:TOP_N]
    return {
        "files": files,
        "text_files": text_files,
        "binary_files": binary_files,
        "zero_byte_files": zero,
        "binary_files_pct": _pct(binary_files, files),
        "total_bytes": total,
        "binary_bytes": binary_bytes,
        "binary_bytes_pct": _pct(binary_bytes, total),
        "files_over_10mb": large,
        "largest_files": [{"path": p, "bytes": s} for s, p in largest],
        "top_extensions_by_bytes": [
            {"ext": e, "bytes": b, "files": by_ext_files[e]} for e, b in by_ext_bytes.most_common(TOP_N)
        ],
    }


# --------------------------------------------------------------------------- the history


def read_commits(jsonl: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not jsonl.is_file():
        return rows
    with jsonl.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return rows


def change_shape(commits: list[dict[str, Any]]) -> dict[str, Any]:
    """How big a typical change is. Only changes that carry file detail count (merges/integrations and
    changes past the detail cap have no file list)."""
    sized = [c for c in commits if c.get("files")]
    lines = [c["additions"] + c["deletions"] for c in sized]
    nfiles = [len(c["files"]) for c in sized]
    mega = sum(1 for n in lines if n >= MEGA_CHANGE_LINES)
    return {
        "changes_with_detail": len(sized),
        "median_lines_per_change": statistics.median(lines) if lines else None,
        "p90_lines_per_change": _quantile(lines, 0.9),
        "max_lines_in_a_change": max(lines) if lines else None,
        "median_files_per_change": statistics.median(nfiles) if nfiles else None,
        "p90_files_per_change": _quantile(nfiles, 0.9),
        "mega_changes": mega,
        "mega_changes_pct": _pct(mega, len(sized)),
    }


def activity(commits: list[dict[str, Any]], now: datetime | None = None) -> dict[str, Any]:
    """Is the project alive, and how steady is the work."""
    now = now or datetime.now(timezone.utc)
    dated = sorted(
        ((d, c) for c in commits if (d := _parse(c.get("author_date", ""))) is not None and d.timestamp() > 0),
        key=lambda pair: pair[0],  # not the tuple itself: equal dates would compare the dicts
    )
    if not dated:
        return {}
    last90, last365 = now - timedelta(days=90), now - timedelta(days=365)
    months = Counter(d.strftime("%Y-%m") for d, _ in dated)
    gaps = [(b[0] - a[0]).days for a, b in zip(dated, dated[1:])]
    humans = [(d, c) for d, c in dated if not c.get("is_bot")]
    return {
        "commits_last_90d": sum(1 for d, _ in dated if d >= last90),
        "commits_last_365d": sum(1 for d, _ in dated if d >= last365),
        "active_authors_90d": len({c["author_name"] for d, c in humans if d >= last90}),
        "active_authors_365d": len({c["author_name"] for d, c in humans if d >= last365}),
        "active_months": len(months),
        "median_commits_per_active_month": statistics.median(months.values()),
        "longest_gap_days": max(gaps) if gaps else 0,
        "busiest_month": max(months.items(), key=lambda kv: kv[1])[0],
    }


def authorship(commits: list[dict[str, Any]]) -> dict[str, Any]:
    """Concentration of the work: a bus-factor view the author counts alone hide."""
    counts = Counter(c["author_name"] for c in commits if not c.get("is_bot") and c.get("author_name"))
    total = sum(counts.values())
    if not total:
        return {}
    running = needed = 0
    for _, n in counts.most_common():
        running += n
        needed += 1
        if running >= 0.8 * total:
            break
    top_name, top_n = counts.most_common(1)[0]
    return {
        "top_author_share_pct": _pct(top_n, total),
        "authors_for_80pct_of_commits": needed,
        "authors_with_5pct_or_more": sum(1 for n in counts.values() if n / total >= 0.05),
    }


def message_quality(commits: list[dict[str, Any]]) -> dict[str, Any]:
    subjects = [(c.get("subject") or "") for c in commits]
    n = len(subjects)
    generic = sum(1 for s in subjects if _GENERIC_SUBJECT.match(s))
    reviewed = sum(1 for s in subjects if _REVIEW_MARK.search(s))
    return {
        "generic_message_pct": _pct(generic, n),
        "review_tagged_pct": _pct(reviewed, n),
        "median_subject_chars": statistics.median([len(s) for s in subjects]) if subjects else None,
    }


# --------------------------------------------------------------------------- assemble + roll up

ROW_FIELDS = [
    "repo", "vcs", "total_commits", "merge_or_integration_commits", "merge_or_integration_pct",
    "files", "total_bytes", "binary_files_pct", "binary_bytes_pct", "files_over_10mb", "largest_file_bytes",
    "median_lines_per_change", "p90_lines_per_change", "max_lines_in_a_change", "median_files_per_change",
    "mega_changes_pct", "commits_last_90d", "commits_last_365d", "active_authors_90d", "active_authors_365d",
    "active_months", "longest_gap_days", "top_author_share_pct", "authors_for_80pct_of_commits",
    "generic_message_pct", "review_tagged_pct", "branch_or_stream_count", "branch_copy_pct",
    "integration_sources", "has_largefiles", "has_subrepos", "notes",
]


def collect(
    *,
    repo: str,
    vcs: str,
    tree: Path,
    commits_jsonl: Path,
    stats: dict[str, Any],
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Everything above for one repo, ready to be written as ``vcs_extra.json``."""
    commits = read_commits(commits_jsonl)
    inv = tree_inventory(tree) if tree.is_dir() else {}
    merges = stats.get("merge_commits", stats.get("integration_changelists"))
    total = stats.get("total_commits") or len(commits)
    out: dict[str, Any] = {
        "repo": repo,
        "vcs": vcs,
        "total_commits": total,
        "merge_or_integration_commits": merges,
        "merge_or_integration_pct": _pct(merges, total) if merges is not None else None,
        "inventory": inv,
        "change_shape": change_shape(commits),
        "activity": activity(commits),
        "authorship": authorship(commits),
        "messages": message_quality(commits),
        "branch_or_stream_count": stats.get("branch_count", stats.get("stream_count")),
        **(extra or {}),
    }
    return out


def flatten(rec: dict[str, Any]) -> dict[str, Any]:
    inv, shape, act = rec.get("inventory") or {}, rec.get("change_shape") or {}, rec.get("activity") or {}
    auth, msg = rec.get("authorship") or {}, rec.get("messages") or {}
    largest = (inv.get("largest_files") or [{}])[0].get("bytes")
    row = {
        "repo": rec["repo"], "vcs": rec["vcs"], "total_commits": rec.get("total_commits"),
        "merge_or_integration_commits": rec.get("merge_or_integration_commits"),
        "merge_or_integration_pct": rec.get("merge_or_integration_pct"),
        "files": inv.get("files"), "total_bytes": inv.get("total_bytes"),
        "binary_files_pct": inv.get("binary_files_pct"), "binary_bytes_pct": inv.get("binary_bytes_pct"),
        "files_over_10mb": inv.get("files_over_10mb"), "largest_file_bytes": largest,
        "branch_or_stream_count": rec.get("branch_or_stream_count"),
        "branch_copy_pct": rec.get("branch_copy_pct"),
        "integration_sources": "; ".join(rec.get("integration_sources") or []),
        "has_largefiles": rec.get("has_largefiles"), "has_subrepos": rec.get("has_subrepos"),
        "notes": rec.get("notes", ""),
    }
    for src, keys in (
        (shape, ["median_lines_per_change", "p90_lines_per_change", "max_lines_in_a_change",
                 "median_files_per_change", "mega_changes_pct"]),
        (act, ["commits_last_90d", "commits_last_365d", "active_authors_90d", "active_authors_365d",
               "active_months", "longest_gap_days"]),
        (auth, ["top_author_share_pct", "authors_for_80pct_of_commits"]),
        (msg, ["generic_message_pct", "review_tagged_pct"]),
    ):
        for k in keys:
            row[k] = src.get(k)
    return row


def write_run_csv(run_dir: Path, records: Iterable[dict[str, Any]]) -> Path | None:
    rows = [flatten(r) for r in records]
    if not rows:
        return None
    path = run_dir / "vcs_extra_metrics.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=ROW_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(sorted(rows, key=lambda r: r["repo"].lower()))
    return path


def load_run_records(run_dir: Path) -> list[dict[str, Any]]:
    out = []
    for p in sorted((run_dir / "git").glob("*/vcs_extra.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return out
