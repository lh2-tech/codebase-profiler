"""Commit-hash deduplication across the repos of one run.

For every repo we keep only the *hashes* of its commits (``git/<repo>/commit_hashes.txt.gz``); no messages, authors,
paths or content ever reach this module. Once all repos are done, ``compute`` answers, per repo, what share of its
commits also exist in at least one other repo of the run (a copy, a fork, a mirror, a repo split off another).

Definitions (one place, so the README, the CSV columns and the tests cannot drift apart):
  commits(A)         every hash reachable from any ref of A (``git rev-list --all``), not capped by
                     EXTRACT_COMMIT_DETAIL_LIMIT.
  duplicate_commits  |commits(A) intersect union(commits(B) for every other repo B)|
  duplicate_pct      100 * duplicate_commits / |commits(A)|, half-up to one decimal. Blank when A has no commits,
                     when its hashes could not be collected, or when fewer than two repos have commits.
  pair (A, B)        shared = |commits(A) intersect commits(B)|, with the share of each side.
"""

from __future__ import annotations

import csv
import gzip
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

HASH_FILE_NAME = "commit_hashes.txt.gz"
DUPLICATES_FILE_NAME = "duplicates.csv"
DEDUP_FIELDS = ["duplicate_pct", "duplicate_commits", "duplicates_with"]
PAIR_FIELDS = ["repo_a", "repo_b", "shared_commits", "pct_of_a", "pct_of_b"]
# How many other repos `duplicates_with` names. The full picture is in duplicates.csv.
TOP_N_OTHERS = 3
SEPARATOR = "; "

_HASH = re.compile(r"[0-9a-f]{40}")


@dataclass(frozen=True)
class Pair:
    repo_a: str
    repo_b: str
    shared_commits: int
    pct_of_a: float
    pct_of_b: float


@dataclass(frozen=True)
class RepoDup:
    commits: int
    # None = unknown (blank in the CSV), never 0: see the module docstring.
    duplicate_commits: int | None
    duplicate_pct: float | None
    top: list[tuple[str, float]] = field(default_factory=list)


@dataclass(frozen=True)
class DedupResult:
    stats: dict[str, RepoDup]
    pairs: list[Pair]

    @property
    def repos_compared(self) -> int:
        return sum(1 for s in self.stats.values() if s.duplicate_pct is not None)

    @property
    def repos_sharing(self) -> int:
        return sum(1 for s in self.stats.values() if s.duplicate_commits)


def _pct(part: int, whole: int) -> float:
    # Integer half-up: float round() is banker's rounding and 6.25 would print as 6.2.
    return ((2000 * part + whole) // (2 * whole)) / 10


def _to_bytes(value: str) -> bytes:
    if not _HASH.fullmatch(value):
        raise ValueError(f"not a 40-character lowercase hex commit hash: {value[:60]!r}")
    return bytes.fromhex(value)


def compute(hash_sources: dict[str, Iterable[str]]) -> DedupResult:
    """Compare repos by commit hash. Each source is read exactly once and may be a lazy generator.

    Runs in one pass over every hash: an inverted index maps each hash to the repo(s) holding it, so the cost is
    linear in the total number of commits, not quadratic in the number of repos. Repos are processed in sorted name
    order and the index only ever appends, so the result does not depend on dict order or worker count.
    """
    names = sorted(hash_sources)
    # hash (20 bytes, about half the size of the hex string) -> repo index, or list of indexes once a second repo has it
    index: dict[bytes, int | list[int]] = {}
    sizes: list[int] = []
    for position, name in enumerate(names):
        count = 0
        for text in hash_sources[name]:
            key = _to_bytes(text)
            held = index.get(key)
            if held is None:
                index[key] = position
                count += 1
            elif isinstance(held, int):
                if held != position:
                    index[key] = [held, position]
                    count += 1
            elif held[-1] != position:  # repos load one at a time, so this repo's own entry would be last
                held.append(position)
                count += 1
        sizes.append(count)

    # Many commits are held by exactly the same set of repos (every commit of a mirror): count each distinct set once
    # and expand it, instead of expanding every commit.
    holders: Counter[tuple[int, ...]] = Counter(tuple(h) for h in index.values() if isinstance(h, list))
    del index
    duplicates = [0] * len(names)
    shared: Counter[tuple[int, int]] = Counter()
    for members, commits in holders.items():
        for i, a in enumerate(members):
            duplicates[a] += commits
            for b in members[i + 1:]:
                shared[(a, b)] += commits

    comparable = sum(1 for size in sizes if size > 0) >= 2
    others: list[list[tuple[int, int]]] = [[] for _ in names]
    for (a, b), count in shared.items():
        others[a].append((count, b))
        others[b].append((count, a))

    stats: dict[str, RepoDup] = {}
    for position, name in enumerate(names):
        size = sizes[position]
        if not comparable or size == 0:
            stats[name] = RepoDup(commits=size, duplicate_commits=None, duplicate_pct=None)
            continue
        ranked = sorted(others[position], key=lambda item: (-item[0], names[item[1]]))[:TOP_N_OTHERS]
        stats[name] = RepoDup(
            commits=size,
            duplicate_commits=duplicates[position],
            duplicate_pct=_pct(duplicates[position], size),
            top=[(names[other], _pct(count, size)) for count, other in ranked],
        )
    pairs = [
        Pair(names[a], names[b], count, _pct(count, sizes[a]), _pct(count, sizes[b]))
        for (a, b), count in shared.items()
    ]
    pairs.sort(key=lambda p: (-p.shared_commits, p.repo_a, p.repo_b))
    return DedupResult(stats=stats, pairs=pairs)


# ── summary.csv columns ─────────────────────────────────────────────────────

def blank_rows(rows: list[dict[str, Any]]) -> None:
    """Empty the dedup columns, e.g. so values from an earlier run cannot survive a run that does not compare."""
    for row in rows:
        for column in DEDUP_FIELDS:
            row[column] = ""


def apply_to_rows(
    rows: list[dict[str, Any]],
    result: DedupResult,
    name_of_row: Callable[[dict[str, Any]], str | None],
) -> None:
    """Set the three columns on every row; a row whose repo has no (comparable) hashes is left blank."""
    blank_rows(rows)
    for row in rows:
        name = name_of_row(row)
        stats = result.stats.get(name) if name is not None else None
        if stats is None or stats.duplicate_pct is None:
            continue
        row["duplicate_pct"] = f"{stats.duplicate_pct:.1f}"
        row["duplicate_commits"] = stats.duplicate_commits
        row["duplicates_with"] = SEPARATOR.join(f"{other}:{pct:.1f}" for other, pct in stats.top)


def write_duplicates_csv(path: Path, pairs: list[Pair]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(PAIR_FIELDS)
        for pair in pairs:
            writer.writerow(
                [pair.repo_a, pair.repo_b, pair.shared_commits, f"{pair.pct_of_a:.1f}", f"{pair.pct_of_b:.1f}"]
            )


# ── per-repo hash files ─────────────────────────────────────────────────────

def hash_file_path(run_dir: Path, slug: str) -> Path:
    return run_dir / "git" / slug / HASH_FILE_NAME


def write_hash_file(path: Path, hashes: Iterable[str]) -> int:
    """Write the unique hashes sorted, one per line, gzip. Atomic (temp + rename) so a crash never leaves a
    half-written list that would later read as a smaller repo. Returns the number of commits written."""
    unique = sorted(set(hashes))
    for value in unique:
        if not _HASH.fullmatch(value):
            raise ValueError(f"not a 40-character lowercase hex commit hash: {value[:60]!r}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    try:
        # mtime=0 and no file name in the header: the same hashes always give the same bytes
        with open(temp, "wb") as raw, gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as gz:
            gz.write("".join(f"{value}\n" for value in unique).encode("ascii"))
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()
    return len(unique)


def iter_hash_file(path: Path) -> Iterator[str]:
    with gzip.open(path, "rt", encoding="ascii") as handle:
        for line in handle:
            value = line.rstrip("\n")
            if not _HASH.fullmatch(value):
                raise ValueError(f"{path}: line is not a 40-character lowercase hex commit hash: {value[:60]!r}")
            yield value


def hash_sources(run_dir: Path, names_by_slug: dict[str, str]) -> dict[str, Iterable[str]]:
    """Lazy hash sources, keyed by display name, for the repos of this run that have a hash file."""
    sources: dict[str, Iterable[str]] = {}
    for slug, name in names_by_slug.items():
        path = hash_file_path(run_dir, slug)
        if path.is_file():
            sources[name] = _FileHashes(path)
    return sources


class _FileHashes:
    """Re-readable on purpose: compute() reads once, but a generator would make a second look silently empty."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def __iter__(self) -> Iterator[str]:
        return iter_hash_file(self.path)
