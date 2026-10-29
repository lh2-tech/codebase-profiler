"""Per-PR open-source and AI-signature shares, from git history only.

Every merged PR/MR is located by its merge or squash commit (the same markers ``detect_merged_prs_from_git`` uses,
so PRs rebased onto the branch without any marker are not seen). For each PR we count changed lines in code files
(added + deleted, the same code extensions as the repo-level LOC metrics):

  oss_loc        changed lines in files with strong open-source evidence (``is_oss_path``, the same rule as
                 ``open_source_loc_pct``: lockfile-backed dependency trees, OSS LICENSE subtrees, SPDX headers)
  ai_loc         changed lines in the PR's commits that carry an AI-tool signature: a Co-Authored-By / Generated-with
                 trailer, or a known AI agent as author or committer. A squash commit is one unit, so it is all or
                 nothing; a merge commit is judged commit by commit.
  changed_loc    all changed code lines of the PR

A PR is *affected* when its share is above 0 and *majority* when it is above 50%. Only numbers, PR numbers and the PR's
merge/squash commit hash are persisted (``git/<repo>/pr_signals.json``): no paths, messages or authors.
Nothing here claims a line was written by an AI; a signature is evidence of tool use, not of authorship.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

FILE_NAME = "pr_signals.json"
MAJORITY_PCT = 50.0
# Budget for one repo. A partial walk would undercount, so on overrun the whole result is dropped (blank fields).
DEFAULT_BUDGET_SECONDS = 300.0
# Commits listed per merge PR; a PR with more is judged on the first ones only.
MAX_COMMITS_PER_PR = 500

AI_MESSAGE_RE = re.compile(
    r"co-authored-by:[^\n]*(?:claude|copilot|codex|cursor|gemini|aider|devin|openai|chatgpt|anthropic|windsurf)"
    r"|generated\s+(?:with|by)\s+(?:\[?claude|claude\s+code|cursor|github\s+copilot|copilot|codex|gemini|aider|windsurf)"
    r"|\baider:\s"
    r"|\U0001F916\s*generated",
    re.I,
)
AI_AUTHOR_RE = re.compile(
    r"copilot-swe-agent|copilot\[bot\]|cursoragent|cursor\s*agent|devin-ai|\bclaude\b|\baider\b|openai-codex|codex\[bot\]",
    re.I,
)


@dataclass(frozen=True)
class PrSignal:
    number: str
    commit: str
    changed_loc: int
    oss_loc: int
    ai_loc: int
    ai_total_loc: int

    def as_json(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "commit": self.commit,
            "changed_loc": self.changed_loc,
            "oss_loc": self.oss_loc,
            "ai_loc": self.ai_loc,
            "ai_total_loc": self.ai_total_loc,
        }


@dataclass(frozen=True)
class Tally:
    prs: int
    affected: int
    majority: int


def _is_majority(part: int, whole: int) -> bool:
    return whole > 0 and 100.0 * part / whole > MAJORITY_PCT


def tally(signals: Iterable[PrSignal], kind: str) -> Tally:
    """kind: "oss" or "ai"."""
    prs = affected = majority = 0
    for sig in signals:
        part, whole = (sig.oss_loc, sig.changed_loc) if kind == "oss" else (sig.ai_loc, sig.ai_total_loc)
        prs += 1
        if part > 0:
            affected += 1
        if _is_majority(part, whole):
            majority += 1
    return Tally(prs, affected, majority)


def carries_ai_signature(author: str, message: str) -> bool:
    return bool(AI_AUTHOR_RE.search(author) or AI_MESSAGE_RE.search(message))


def _numstat(text: str, is_code: Callable[[str], bool]) -> list[tuple[str, int]]:
    """(path, added + deleted) for every code file in ``git --numstat`` output; binary files ("-") are skipped."""
    out: list[tuple[str, int]] = []
    for line in text.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        if is_code(parts[2]):
            out.append((parts[2], int(parts[0]) + int(parts[1])))
    return out


def analyse(
    records: list[dict[str, str]],
    git: Callable[..., str],
    *,
    is_oss_path: Callable[[str], bool],
    is_code: Callable[[str], bool],
    budget_seconds: float = DEFAULT_BUDGET_SECONDS,
    now: Callable[[], float] = time.monotonic,
) -> list[PrSignal] | None:
    """One PrSignal per record that has a merge/squash commit. ``git(*args)`` returns stdout ("" on failure).
    Returns None when the time budget ran out, so a partial walk is never published as the repo's figure."""
    deadline = now() + budget_seconds
    signals: list[PrSignal] = []
    for record in records:
        sha = record.get("merge_commit") or ""
        if not sha:
            continue
        if now() > deadline:
            return None
        parents = git("rev-list", "--parents", "-n", "1", sha).split()[1:]
        if len(parents) >= 2:
            net = _numstat(git("diff", "--numstat", "--no-renames", parents[0], sha), is_code)
            units = _merge_units(git, parents, sha, is_code)
        else:
            net = _numstat(git("show", "--numstat", "--format=", "--no-renames", sha), is_code)
            head = git("show", "-s", "--format=%an <%ae>%x1f%cn <%ce>%x1f%B", sha)
            author, _, rest = head.partition("\x1f")
            committer, _, message = rest.partition("\x1f")
            flagged = carries_ai_signature(f"{author} {committer}", message)
            total = sum(n for _, n in net)
            units = [(total, flagged)]
        changed = sum(n for _, n in net)
        oss = sum(n for path, n in net if is_oss_path(path))
        ai_total = sum(n for n, _ in units)
        ai = sum(n for n, flagged in units if flagged)
        signals.append(PrSignal(record.get("pr_number") or sha[:12], sha, changed, oss, ai, ai_total))
    return signals


def _merge_units(
    git: Callable[..., str], parents: list[str], sha: str, is_code: Callable[[str], bool]
) -> list[tuple[int, bool]]:
    """(changed code lines, carries AI signature) per non-merge commit brought in by merge ``sha``."""
    text = git(
        "log", "--no-merges", f"--max-count={MAX_COMMITS_PER_PR}", "--no-renames", "--numstat",
        "--format=%x1e%an <%ae>%x1f%cn <%ce>%x1f%B%x1f", f"{parents[0]}..{parents[1]}",
    )
    units: list[tuple[int, bool]] = []
    for chunk in text.split("\x1e"):
        if not chunk.strip():
            continue
        fields = chunk.split("\x1f", 3)
        if len(fields) < 4:
            continue
        author, committer, message, stat = fields
        total = sum(n for _, n in _numstat(stat, is_code))
        units.append((total, carries_ai_signature(f"{author} {committer}", message)))
    return units


# ── summary.csv fields ──────────────────────────────────────────────────────

_SUFFIX = re.compile(r",(\d+)(?:,>50%:(\d+))?\s*$")


def with_pr_counts(existing: Any, counts: Tally | None) -> Any:
    """``<value>,<PRs affected>`` and, when any PR is more than half affected, ``,>50%:<how many>``.

    ``existing`` is the repo-level value (a percentage, or the LLM's JSON array of signals). It is kept verbatim in
    front of the first comma; a blank one is shown as ``-`` so the field never starts with a comma.
    Returns ``existing`` untouched when the PRs could not be analysed."""
    if counts is None:
        return existing
    text = "-" if existing in ("", None) else str(existing)
    out = f"{text},{counts.affected}"
    if counts.majority:
        out += f",>50%:{counts.majority}"
    return out


def strip_pr_counts(value: Any) -> str:
    """The repo-level part of a field written by ``with_pr_counts`` (no PR counts)."""
    return _SUFFIX.sub("", str(value or ""))


def read_pr_counts(value: Any) -> tuple[int, int] | None:
    """(PRs affected, PRs more than half affected) from a field written by ``with_pr_counts``; None if absent."""
    match = _SUFFIX.search(str(value or ""))
    if not match:
        return None
    return int(match.group(1)), int(match.group(2) or 0)


# ── per-repo file ───────────────────────────────────────────────────────────

def file_path(run_dir: Path, slug: str) -> Path:
    return run_dir / "git" / slug / FILE_NAME


def read_file(path: Path) -> list[PrSignal]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [PrSignal(**item) for item in data]


def pr_commit_sources(run_dir: Path, names_by_slug: dict[str, str]) -> dict[str, Iterable[str]]:
    """Merge/squash commit hashes of each repo's PRs, for the duplicate-PR count. Repos without a file are left out."""
    sources: dict[str, Iterable[str]] = {}
    for slug, name in names_by_slug.items():
        path = file_path(run_dir, slug)
        if path.is_file():
            sources[name] = [sig.commit for sig in read_file(path)]
    return sources
