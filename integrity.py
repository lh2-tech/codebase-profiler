#!/usr/bin/env python3
"""Tamper-evidence for a profiler run.

Three layers, none of which sends anything off the machine:

1. ``git/<repo>/integrity.json`` -- facts read straight from the repository while it still exists: the measured
   branch's HEAD commit and tree id, root commits, a digest of every ref tip, ``git fsck`` result, how many commits
   are merges / empty / author!=committer, and the PR-merge numbering. Anyone holding the repository can recompute
   the commit and tree ids, so these cannot be edited in the CSV without being caught later.
2. ``INTEGRITY.json`` -- written last, before the zip: SHA-256 of every output file, one chain digest over them,
   the SHA-256 of the profiler's own ``*.py`` files (compare against the version you handed out), an optional
   challenge string you gave the operator, and an optional HMAC when ``PROFILER_INTEGRITY_KEY`` is set.
3. ``verify`` -- re-hashes a run folder or zip, checks the HMAC and challenge, cross-checks ``summary.csv`` against
   the per-repo files, and prints red flags about the shape of the history.

Limit: a profiler run on the operator's machine proves consistency, not honesty. Whoever controls the machine and
the key can regenerate every file. The independent check is a spot re-profile of repositories you hold: the
``head_tree`` / ``head_commit`` values in integrity.json must match.

    python integrity.py verify raw-extract-ORG-STAMP[.zip] [--key KEY] [--challenge TEXT]
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import hmac
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = 1
REPO_FILE = "integrity.json"
RUN_FILE = "INTEGRITY.json"
KEY_ENV = "PROFILER_INTEGRITY_KEY"
PR_MERGE_RE = re.compile(r"^Merge pull request #(\d+)")


def _git(repo: Path, *args: str, timeout: int = 600) -> tuple[int, str]:
    proc = subprocess.run(
        ["git", "-c", "safe.directory=*", "-C", str(repo), *args],
        capture_output=True, text=True, timeout=timeout,
    )
    return proc.returncode, proc.stdout


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _resolve_ref(repo: Path, branch: str) -> str:
    """The first of refs/heads/B, refs/remotes/origin/B, B that exists; ``HEAD`` when nothing was named."""
    for ref in ([f"refs/heads/{branch}", f"refs/remotes/origin/{branch}", branch] if branch else []):
        code, _ = _git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
        if code == 0:
            return ref
    return "HEAD"


def repo_facts(repo: Path, branch: str = "") -> dict:
    """Facts about ``repo`` on the measured branch. Never raises: a failure becomes an ``error`` field."""
    facts: dict = {"schema": SCHEMA, "branch": branch, "git_version": ""}
    try:
        facts["git_version"] = _git(repo, "--version")[1].strip()
        ref = _resolve_ref(repo, branch)
        facts["ref"] = ref
        facts["head_commit"] = _git(repo, "rev-parse", f"{ref}^{{commit}}")[1].strip()
        facts["head_tree"] = _git(repo, "rev-parse", f"{ref}^{{tree}}")[1].strip()
        roots = _git(repo, "rev-list", "--max-parents=0", ref)[1].split()
        facts["root_commits"] = sorted(roots)

        refs = _git(repo, "for-each-ref", "--format=%(refname) %(objectname)")[1].splitlines()
        keep = [r for r in refs if r.startswith(("refs/heads/", "refs/tags/", "refs/remotes/"))]
        pulls = [r for r in refs if r.startswith("refs/pull/")]
        facts["ref_count"] = len(keep)
        facts["refs_sha256"] = hashlib.sha256("\n".join(sorted(keep)).encode()).hexdigest()
        facts["pull_ref_count"] = len(pulls)

        code, out = _git(repo, "rev-parse", "--is-shallow-repository")
        facts["shallow"] = out.strip() == "true"
        code, out = _git(repo, "fsck", "--connectivity-only", "--no-dangling")
        facts["fsck_ok"] = code == 0
        if code != 0:
            facts["fsck_note"] = out.strip()[:200]
        for line in _git(repo, "count-objects", "-v")[1].splitlines():
            key, _, val = line.partition(": ")
            if key in ("count", "in-pack", "size-pack"):
                facts[f"objects_{key.replace('-', '_')}"] = int(val) if val.isdigit() else val

        # one pass over the measured history: ids, parents, tree, author/committer date, subject
        log = _git(repo, "log", ref, "--format=%H|%T|%P|%aI|%cI|%s")[1].splitlines()
        trees: dict[str, str] = {}
        rows = []
        for line in log:
            h, tree, parents, adate, cdate, subject = line.split("|", 5)
            trees[h] = tree
            rows.append((h, tree, parents.split(), adate, cdate, subject))
        merges = sum(1 for r in rows if len(r[2]) > 1)
        empty = sum(1 for r in rows if len(r[2]) == 1 and trees.get(r[2][0]) == r[1])
        non_merge = len(rows) - merges
        facts["commits"] = len(rows)
        facts["merge_commits"] = merges
        facts["empty_commits"] = empty
        facts["empty_commit_pct"] = round(100 * empty / non_merge, 1) if non_merge else 0.0
        facts["author_eq_committer_date_pct"] = (
            round(100 * sum(1 for r in rows if r[3] == r[4]) / len(rows), 1) if rows else 0.0
        )
        stamps = Counter(r[3] for r in rows)
        facts["same_timestamp_max_pct"] = (
            round(100 * stamps.most_common(1)[0][1] / len(rows), 1) if rows else 0.0
        )
        prs = sorted({int(m.group(1)) for r in rows if (m := PR_MERGE_RE.match(r[5]))})
        facts["pr_merge_numbers"] = len(prs)
        facts["pr_number_max"] = prs[-1] if prs else 0
        facts["pr_number_gaps"] = (prs[-1] - len(prs)) if prs else 0
    except Exception as exc:  # noqa: BLE001 - integrity facts must never stop a run
        facts["error"] = f"{type(exc).__name__}: {exc}"[:300]
    return facts


def write_repo_integrity(repo: Path, git_dir: Path, branch: str, log=None) -> None:
    facts = repo_facts(repo, branch)
    hashes = git_dir / "commit_hashes.txt.gz"
    if hashes.exists():
        facts["commit_hashes_sha256"] = sha256_file(hashes)
    git_dir.mkdir(parents=True, exist_ok=True)
    (git_dir / REPO_FILE).write_text(json.dumps(facts, indent=2), encoding="utf-8")
    if log is not None and facts.get("error"):
        log.warning("integrity facts incomplete for %s: %s", repo, facts["error"])


# ── run level ────────────────────────────────────────────────────────────────

def _tool_hashes() -> dict[str, str]:
    here = Path(__file__).resolve().parent
    return {p.name: sha256_file(p) for p in sorted(here.glob("*.py")) if not p.name.startswith("test_")}


def _run_files(run_dir: Path) -> dict[str, str]:
    out = {}
    for path in sorted(run_dir.rglob("*")):
        if not path.is_file() or path.name == RUN_FILE:
            continue
        rel = path.relative_to(run_dir).as_posix()
        if "clones/" in rel or rel == "logs/extract.log":  # the log is still appended to after this runs
            continue
        out[rel] = sha256_file(path)
    return out


def _chain(files: dict[str, str]) -> str:
    return hashlib.sha256("\n".join(f"{h}  {p}" for p, h in sorted(files.items())).encode()).hexdigest()


def _sign(body: dict, key: str) -> str:
    return hmac.new(key.encode(), json.dumps(body, sort_keys=True).encode(), hashlib.sha256).hexdigest()


def write_run_integrity(run_dir: Path, challenge: str = "", log=None) -> Path:
    files = _run_files(run_dir)
    body = {
        "schema": SCHEMA,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "challenge": challenge,
        "file_count": len(files),
        "chain_sha256": _chain(files),
        "files": files,
        "tool_sha256": _tool_hashes(),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
    }
    key = os.environ.get(KEY_ENV, "")
    if key:
        body["hmac_sha256"] = _sign({k: v for k, v in body.items() if k != "hmac_sha256"}, key)
    path = run_dir / RUN_FILE
    path.write_text(json.dumps(body, indent=2), encoding="utf-8")
    if log is not None:
        log.info("Wrote %s (%d files, chain %s…%s)", path, len(files), body["chain_sha256"][:12],
                 " signed" if key else " unsigned")
    return path


# ── verification ─────────────────────────────────────────────────────────────

def _locate(path: Path) -> tuple[Path, tempfile.TemporaryDirectory | None]:
    if path.is_file() and path.suffix == ".zip":
        tmp = tempfile.TemporaryDirectory(prefix="integrity-verify-")
        with zipfile.ZipFile(path) as zf:
            zf.extractall(tmp.name)
        roots = [p for p in Path(tmp.name).iterdir() if p.is_dir()]
        return (roots[0] if len(roots) == 1 else Path(tmp.name)), tmp
    return path, None


def _num(value) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def verify_run(path: Path, key: str = "", challenge: str = "") -> dict:
    """Returns ``{"problems": [...], "flags": [...], "repos": n}``. Problems are hard failures (tampering or damage);
    flags are patterns worth asking about (generated-looking history)."""
    run_dir, tmp = _locate(path)
    problems: list[str] = []
    flags: list[str] = []
    try:
        manifest_path = run_dir / RUN_FILE
        if not manifest_path.exists():
            problems.append(f"{RUN_FILE} is missing: this run carries no tamper evidence")
            body: dict = {}
        else:
            body = json.loads(manifest_path.read_text(encoding="utf-8"))
            files = _run_files(run_dir)
            recorded = body.get("files", {})
            for rel, digest in recorded.items():
                if rel not in files:
                    problems.append(f"missing file: {rel}")
                elif files[rel] != digest:
                    problems.append(f"modified file: {rel}")
            for rel in files.keys() - recorded.keys():
                problems.append(f"file not in manifest (added later): {rel}")
            if body.get("chain_sha256") != _chain(recorded):
                problems.append("chain digest does not match the recorded file list")
            if body.get("hmac_sha256"):
                if not key:
                    flags.append("manifest is signed but no --key was given: signature not checked")
                elif not hmac.compare_digest(
                    body["hmac_sha256"], _sign({k: v for k, v in body.items() if k != "hmac_sha256"}, key)
                ):
                    problems.append("HMAC signature is wrong: manifest was not produced with this key")
            elif key:
                problems.append("a key was given but the manifest is unsigned")
            if challenge and body.get("challenge") != challenge:
                problems.append("challenge string does not match the one issued for this run")

        summary = run_dir / "summary.csv"
        rows = list(csv.DictReader(summary.open(encoding="utf-8", newline=""))) if summary.exists() else []
        if not rows:
            problems.append("summary.csv is missing or empty")
        for row in rows:
            repo = row.get("repo", "")
            d = run_dir / "git" / repo
            if not d.is_dir():
                problems.append(f"{repo}: no git/ folder")
                continue
            try:
                stats = json.loads((d / "git_stats.json").read_text(encoding="utf-8"))
                scc = json.loads((d / "scc.json").read_text(encoding="utf-8"))
                hash_file = d / "commit_hashes.txt.gz"
                hashes = gzip.open(hash_file, "rt").read().split() if hash_file.exists() else None
                commits = [json.loads(line) for line in (d / "commits.jsonl").open(encoding="utf-8")]
            except Exception as exc:  # noqa: BLE001
                problems.append(f"{repo}: unreadable evidence file ({type(exc).__name__})")
                continue
            if str(scc.get("loc")) != str(row.get("loc")):
                problems.append(f"{repo}: summary loc {row.get('loc')} != scc.json {scc.get('loc')}")
            if sum((scc.get("by_language") or {}).values()) != scc.get("loc"):
                problems.append(f"{repo}: scc.json language lines do not add up to its total")
            for stat_key, col in (("total_commits", "total_commits"), ("first_commit", "first_commit"),
                                  ("last_commit", "last_commit_date"), ("span_days", "span_days")):
                if str(stats.get(stat_key)) != str(row.get(col)):
                    problems.append(f"{repo}: summary {col}={row.get(col)} != git_stats {stats.get(stat_key)}")
            if hashes is None:
                flags.append(f"{repo}: no commit_hashes.txt.gz (run had a single repo or --no-dedup)")
                hashes = [c["sha"] for c in commits]
            elif len(hashes) != _num(row.get("total_commits")) or len(set(hashes)) != len(hashes):
                problems.append(f"{repo}: commit_hashes.txt.gz does not match total_commits / has duplicates")
            if commits and not {c["sha"] for c in commits} <= set(hashes):
                problems.append(f"{repo}: commits.jsonl has hashes that are not in commit_hashes.txt.gz")
            if sum(a.get("commits", 0) for a in stats.get("authors", [])) != stats.get("total_commits"):
                problems.append(f"{repo}: author commit counts do not add up to total_commits")

            facts_path = d / REPO_FILE
            if not facts_path.exists():
                flags.append(f"{repo}: no integrity.json (older profiler)")
                continue
            facts = json.loads(facts_path.read_text(encoding="utf-8"))
            if facts.get("error"):
                flags.append(f"{repo}: integrity facts incomplete: {facts['error']}")
                continue
            if facts.get("commits") != len(hashes):
                problems.append(f"{repo}: repo has {facts.get('commits')} commits, hash file lists {len(hashes)}")
            if facts.get("head_commit") and facts["head_commit"] not in set(hashes):
                problems.append(f"{repo}: measured HEAD commit is not in commit_hashes.txt.gz")
            if facts.get("commit_hashes_sha256") and (d / "commit_hashes.txt.gz").exists() and hashlib.sha256(
                (d / "commit_hashes.txt.gz").read_bytes()
            ).hexdigest() != facts["commit_hashes_sha256"]:
                problems.append(f"{repo}: commit_hashes.txt.gz changed after it was recorded")
            if not facts.get("fsck_ok", True):
                problems.append(f"{repo}: git fsck failed on the source repository")
            if facts.get("shallow"):
                flags.append(f"{repo}: shallow clone, history is cut")
            # shape of the history
            pr_total = facts.get("pr_merge_numbers", 0)
            if pr_total >= 50 and facts.get("pr_number_gaps") == 0:
                flags.append(f"{repo}: {pr_total} PR merges numbered 1..{facts.get('pr_number_max')} with no gaps")
            if pr_total >= 50 and facts.get("pull_ref_count") == pr_total:
                flags.append(f"{repo}: every PR ref is a merged PR (none closed or open)")
            if facts.get("empty_commit_pct", 0) >= 15:
                flags.append(f"{repo}: {facts['empty_commit_pct']}% of non-merge commits change no files")
            if facts.get("author_eq_committer_date_pct", 0) >= 99.5 and facts.get("commits", 0) > 100:
                flags.append(f"{repo}: author date equals committer date on every commit")
            if facts.get("same_timestamp_max_pct", 0) >= 10:
                flags.append(f"{repo}: {facts['same_timestamp_max_pct']}% of commits share one timestamp")
        ratios = [_num(r.get("merged_prs")) / _num(r.get("total_commits")) for r in rows if _num(r.get("total_commits"))]
        if len(ratios) >= 10 and max(ratios) - min(ratios) < 0.02 and ratios[0] > 0:
            flags.append(f"run: merged PRs are {min(ratios):.1%}-{max(ratios):.1%} of commits in every repo")
        return {"problems": problems, "flags": flags, "repos": len(rows)}
    finally:
        if tmp is not None:
            tmp.cleanup()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify a codebase-profiler run")
    sub = parser.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("verify", help="check a run folder or zip")
    v.add_argument("path", type=Path)
    v.add_argument("--key", default=os.environ.get(KEY_ENV, ""))
    v.add_argument("--challenge", default="")
    args = parser.parse_args(argv)
    result = verify_run(args.path, key=args.key, challenge=args.challenge)
    print(f"repos checked: {result['repos']}")
    print(f"problems (tampering / damage): {len(result['problems'])}")
    for p in result["problems"][:50]:
        print(f"  PROBLEM  {p}")
    print(f"flags (generated-looking history): {len(result['flags'])}")
    for f in result["flags"][:50]:
        print(f"  flag     {f}")
    return 1 if result["problems"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
