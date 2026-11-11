"""Plastic SCM (Unity Version Control) history metrics, shaped like the Git ones in extract_org_raw_data.

A Plastic repository lives on a server, so every call goes through the ``cm`` CLI with an explicit
repository spec (``repo@host:8087``). Like the Perforce module this returns the structures the Git
path produces (``git_stats.json``, ``commits.jsonl``), so the summary columns and the archive are
identical for every VCS. Only change metadata and line counts are returned, never file contents.

Credentials. ``cm`` has no way to read a password from the environment, so a run configures a
private client config (``cm configure --clientconf``) under a throw-away ``HOME`` and deletes it at
the end. The password is on that one ``cm configure`` command line for a moment (cm offers no stdin
form); it never goes into repos.json, logs or the archive. With no password the user's own
already-configured ``cm`` client is used unchanged.

Scope. A target is one repository. "Commits" are all changesets on every branch (as for hg). Plastic
has no pull requests: a changeset that is the *destination* of a merge link (``cm find merge``) is
the equivalent of a Git merge commit, so those are what ``merged_prs`` counts and, as with a Git
merge, they are written without a file list so a branch merge does not look like authored lines.

Line counts. ``cm diff cs:N`` lists the changed items; per-file lines come from ``cm diffmetrics``
(changed + added / changed + deleted, so a modified line counts once on each side, as in Git
numstat) and, for an added or removed file, from ``cm cat`` for text files. That is one call per
file, bounded by ``PLASTIC_LINES_LIMIT`` files and ``PLASTIC_CAT_MAX_BYTES`` per file; anything
beyond is left at 0 and reported in ``commits_detail_meta.json``.

Unverified against a live server: this was written from the Unity Version Control CLI reference and
tested against a stub ``cm``. Dates are read as UTC (the server's zone is not exposed by ``cm find``).
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

PLASTIC_LINES_LIMIT = int(os.environ.get("PLASTIC_LINES_LIMIT", "50000"))
PLASTIC_CAT_MAX_BYTES = int(os.environ.get("PLASTIC_CAT_MAX_BYTES", str(2 * 1024 * 1024)))
LINES_WORKERS = 8
DATE_FORMAT = "yyyy-MM-ddTHH:mm:ss"
# Field / record separators for ``--format`` (comments can hold tabs and newlines).
FS = "\x1f"
RS = "\x1e"


class PlasticError(RuntimeError):
    """A cm command failed; the message is safe to show (no credentials in it)."""


@dataclass
class PlasticConn:
    """How to reach one Plastic server. ``password`` empty means: use the user's own cm client config."""

    server: str  # host:port, or an org@cloud name
    user: str = ""
    password: str = field(default="", repr=False)
    workingmode: str = "UPWorkingMode"
    home: str = ""  # private HOME created by ``connect``

    def env(self) -> dict[str, str]:
        env = dict(os.environ)
        if self.home:
            env["HOME"] = self.home
        return env

    def spec(self, repo: str) -> str:
        return f"{repo}@{self.server}"

    def label(self) -> str:
        return f"{self.user or 'default'}@{self.server}"


def cm_available() -> bool:
    return shutil.which("cm") is not None


def _scrub(conn: PlasticConn, text: str) -> str:
    return text.replace(conn.password, "***") if conn.password else text


def slug_for(repo: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", repo).strip("-") or "repo"


# --------------------------------------------------------------------------- running cm


def run_cm(conn: PlasticConn, *args: str, timeout: int = 300, check: bool = True, binary: bool = False) -> Any:
    proc = subprocess.run(["cm", *args], capture_output=True, timeout=timeout, env=conn.env())
    if proc.returncode != 0 and check:
        msg = (proc.stderr or proc.stdout).decode("utf-8", "replace").strip()
        raise PlasticError(f"cm {args[0] if args else ''} failed: {_scrub(conn, msg)[:300]}")
    return proc.stdout if binary else proc.stdout.decode("utf-8", "replace")


def find_records(conn: PlasticConn, obj: str, repo: str, fields: list[str], where: str = "", timeout: int = 900) -> Iterator[list[str]]:
    """``cm find <obj> … --format`` as lists of field values, one per object."""
    fmt = FS.join("{%s}" % f for f in fields) + RS
    args = ["find", obj]
    if where:
        args.append(f"where {where}")
    args += [f"--format={fmt}", f"--dateformat={DATE_FORMAT}", "--nototal", f"on repository '{conn.spec(repo)}'"]
    out = run_cm(conn, *args, timeout=timeout)
    for rec in out.split(RS):
        rec = rec.strip("\r\n")
        if not rec.strip():
            continue
        parts = rec.split(FS)
        if len(parts) == len(fields):
            yield parts


# --------------------------------------------------------------------------- connect


def connect(conn: PlasticConn, log: logging.Logger | None = None) -> None:
    """Configure a private client when a password was given, then prove the server answers."""
    if not cm_available():
        raise PlasticError("`cm` is not installed (Unity Version Control command-line client)")
    if conn.password:
        conn.home = tempfile.mkdtemp(prefix="datalabs-plastic-")
        conf_dir = Path(conn.home, ".plastic4")
        conf_dir.mkdir(mode=0o700)
        args = ["configure", "--language=en", f"--workingmode={conn.workingmode}", f"--user={conn.user}",
                f"--password={conn.password}", f"--server={conn.server}", f"--clientconf={conf_dir / 'client.conf'}"]
        run_cm(conn, *args, timeout=60)
    try:
        run_cm(conn, "repository", "list", conn.server, timeout=60)
    except PlasticError as exc:
        close(conn)
        raise PlasticError(f"cannot list repositories on {conn.server}: {exc}") from exc


def close(conn: PlasticConn) -> None:
    if conn.home:
        shutil.rmtree(conn.home, ignore_errors=True)
        conn.home = ""


def list_repositories(conn: PlasticConn) -> list[str]:
    out = run_cm(conn, "repository", "list", conn.server, "--format={repname}", timeout=60)
    names = [ln.strip() for ln in out.splitlines() if ln.strip()]
    return sorted(n for n in names if not n.lower().startswith(("listing", "total")))


# --------------------------------------------------------------------------- history


def _iso(value: str) -> str:
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).isoformat()
    except ValueError:
        return ""


def identity(owner: str) -> tuple[str, str]:
    owner = owner.strip()
    return owner, owner if "@" in owner else ""


def iter_changesets(conn: PlasticConn, repo: str, timeout: int = 900) -> Iterator[dict[str, Any]]:
    """Every changeset on every branch, newest first."""
    rows = []
    for cid, owner, date, branch, comment in find_records(
        conn, "changeset", repo, ["changesetid", "owner", "date", "branch", "comment"], timeout=timeout
    ):
        try:
            n = int(cid)
        except ValueError:
            continue
        comment = comment.strip()
        rows.append({
            "change": n, "user": owner.strip(), "date": _iso(date), "branch": branch.strip(),
            "desc": comment, "subject": comment.splitlines()[0] if comment else "",
        })
    rows.sort(key=lambda r: -r["change"])
    yield from rows


def merge_destinations(conn: PlasticConn, repo: str, timeout: int = 900) -> set[int]:
    """Changeset ids that are the destination of a merge link."""
    out: set[int] = set()
    try:
        for (dst,) in find_records(conn, "merge", repo, ["dstchangeset"], timeout=timeout):
            if dst.strip().isdigit():
                out.add(int(dst))
    except PlasticError:
        pass
    return out


def aggregate_plastic_stats(
    conn: PlasticConn, repo: str, is_bot: Callable[[str, str], bool],
    log: logging.Logger | None = None, changes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Same keys as ``aggregate_git_stats`` plus Plastic-only extras."""
    total = 0
    first = last = None
    authors: dict[tuple[str, str], int] = {}
    per_user: dict[str, int] = {}
    for c in changes if changes is not None else iter_changesets(conn, repo):
        total += 1
        key = identity(c["user"])
        authors[key] = authors.get(key, 0) + 1
        per_user[c["user"]] = per_user.get(c["user"], 0) + 1
        ts = datetime.fromisoformat(c["date"]) if c["date"] else None
        if ts is None:
            continue
        first = ts if first is None or ts < first else first
        last = ts if last is None or ts > last else last
    rows = sorted(
        ({"name": n, "email": e, "commits": k, "is_bot": bool(is_bot(n, e))} for (n, e), k in authors.items()),
        key=lambda a: (-a["commits"], a["name"].lower()),
    )
    human = [a for a in rows if not a["is_bot"]]
    bots = [a for a in rows if a["is_bot"]]
    bot_commits = sum(a["commits"] for a in bots)
    branches: list[str] = []
    try:
        branches = [b[0].strip() for b in find_records(conn, "branch", repo, ["name"])]
    except PlasticError:
        pass
    return {
        "vcs": "plastic",
        "total_commits": total,
        "first_commit": first.isoformat() if first else None,
        "last_commit": last.isoformat() if last else None,
        "span_days": (last - first).days if first and last else None,
        "recency_days": (datetime.now(timezone.utc) - last).days if last else None,
        "human_authors": len(human),
        "bot_authors": len(bots),
        "bot_commit_count": bot_commits,
        "bot_commit_ratio": round(bot_commits / total, 4) if total else 0.0,
        "authors": rows[:50],
        "commits_per_user": dict(sorted(per_user.items(), key=lambda kv: -kv[1])[:100]),
        "repository": repo,
        "server": conn.server,
        "branch_count": len(branches) if branches else None,
    }


def main_branch(conn: PlasticConn, repo: str) -> str:
    """The branch of changeset 0: the repository's main branch (``main`` or ``/main``)."""
    for (branch,) in find_records(conn, "changeset", repo, ["branch"], where="changesetid = 0"):
        if branch.strip():
            return branch.strip()
    return "main"


# --------------------------------------------------------------------------- per-changeset churn

_DIFF_FIELDS = ["status", "type", "revid", "parentrevid", "path"]


def changeset_items(conn: PlasticConn, repo: str, cs: int) -> list[dict[str, Any]]:
    """Items changed by one changeset vs its parent (directories dropped)."""
    fmt = FS.join("{%s}" % f for f in _DIFF_FIELDS) + RS
    out = run_cm(conn, "diff", f"cs:{cs}@{conn.spec(repo)}", f"--format={fmt}", timeout=600, check=False)
    items = []
    for rec in out.split(RS):
        parts = rec.strip("\r\n").split(FS)
        if len(parts) != len(_DIFF_FIELDS):
            continue
        status, typ, revid, parent, path = (p.strip() for p in parts)
        if typ == "D" or not path:
            continue
        items.append({"status": status, "type": typ, "revid": revid, "parentrevid": parent, "path": path.lstrip("/\\")})
    return items


def export_commits_jsonl(
    conn: PlasticConn, repo: str, out_path: Path, *, changes: list[dict[str, Any]], merges: set[int],
    is_bot: Callable[[str, str], bool], max_commits: int = 0, timeout: int = 1800,
    log: logging.Logger | None = None, write_json: Callable[[Path, Any], None] | None = None,
) -> tuple[int, list[dict[str, str]]]:
    """Per-changeset file churn as JSONL (same record shape as the Git exporter).

    Returns ``(records written, merge changesets)``. ``changes`` is newest first; past ``max_commits``
    only the newest changesets get per-file churn.
    """
    capped = bool(max_commits and len(changes) > max_commits)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    budget = {"left": PLASTIC_LINES_LIMIT, "skipped": 0, "unreadable": 0}
    merge_rows: list[dict[str, str]] = []
    deadline = time.monotonic() + timeout
    spec = conn.spec(repo)

    def is_binary(data: bytes) -> bool:
        return b"\x00" in data[:8000]

    def count_lines(item: dict[str, Any]) -> tuple[int, int]:
        if budget["left"] <= 0:
            budget["skipped"] += 1
            return 0, 0
        budget["left"] -= 1
        if item["type"] == "B":
            return 0, 0
        status = item["status"]
        try:
            if status == "C" and item["parentrevid"] not in ("", "-1"):
                out = run_cm(
                    conn, "diffmetrics", f"revid:{item['parentrevid']}@{spec}", f"revid:{item['revid']}@{spec}",
                    f"--format={{0}}{FS}{{1}}{FS}{{2}}", timeout=120,
                )
                changed, added, deleted = (int(x) for x in out.strip().split(FS))
                return changed + added, changed + deleted
            if status in ("A", "D"):
                rev = item["revid"] if status == "A" else item["parentrevid"]
                if rev in ("", "-1"):
                    return 0, 0
                data = run_cm(conn, "cat", f"revid:{rev}@{spec}", timeout=120, binary=True)
                if len(data) > PLASTIC_CAT_MAX_BYTES:
                    budget["unreadable"] += 1
                    return 0, 0
                if not data or is_binary(data):
                    return 0, 0
                n = data.count(b"\n") + (0 if data.endswith(b"\n") else 1)
                return (n, 0) if status == "A" else (0, n)
        except (PlasticError, subprocess.TimeoutExpired, ValueError):
            budget["unreadable"] += 1
        return 0, 0

    count = 0
    with out_path.open("w", encoding="utf-8") as fh, ThreadPoolExecutor(LINES_WORKERS) as pool:
        for i, c in enumerate(changes):
            if time.monotonic() > deadline:
                raise subprocess.TimeoutExpired("cm diff", timeout)
            if log is not None and i and i % 500 == 0:
                log.info("plastic %s: described %s of %s changesets", repo, f"{i:,}", f"{len(changes):,}")
            name, email = identity(c["user"])
            is_merge = c["change"] in merges
            if is_merge:
                merge_rows.append({"sha": str(c["change"]), "date": c["date"], "author": name,
                                   "subject": c["subject"], "kind": "merge"})
            files: list[dict[str, Any]] = []
            if not is_merge and (not capped or i < max_commits):
                items = changeset_items(conn, repo, c["change"])
                futures = [(it, pool.submit(count_lines, it)) for it in items if it["status"] != "M"]
                files = [{"path": it["path"], "additions": 0, "deletions": 0} for it in items if it["status"] == "M"]
                for it, fut in futures:
                    add, dele = fut.result()
                    files.append({"path": it["path"], "additions": add, "deletions": dele})
                files.sort(key=lambda x: x["path"])
            fh.write(json.dumps({
                "sha": str(c["change"]), "author_name": name, "author_email": email, "author_date": c["date"],
                "subject": c["subject"], "is_bot": bool(is_bot(name, email)), "files": files,
                "additions": sum(f["additions"] for f in files), "deletions": sum(f["deletions"] for f in files),
            }, ensure_ascii=False) + "\n")
            count += 1

    meta = {
        "total_commits": len(changes),
        "detail_commits_exported": min(len(changes), max_commits) if capped else len(changes),
        "capped": capped, "detail_limit": max_commits or None,
        "files_over_budget": budget["skipped"], "files_unreadable_or_too_large": budget["unreadable"],
        "note": (
            "Plastic line counts: cm diffmetrics for changed files (changed+added / changed+deleted), cm cat for "
            "added/removed text files, bounded by PLASTIC_LINES_LIMIT / PLASTIC_CAT_MAX_BYTES. Merge changesets "
            "carry no file list (like a Git merge)."
        ),
    }
    if capped or budget["skipped"] or budget["unreadable"]:
        if log is not None:
            log.warning("commits.jsonl: %s", json.dumps(meta))
        if write_json is not None:
            write_json(out_path.parent / "commits_detail_meta.json", meta)
    return count, merge_rows


# --------------------------------------------------------------------------- working tree


def resolve_local_tree(repo: str, root: str | Path | None = None, overrides: dict[str, str] | None = None) -> Path:
    """The existing workspace folder for ``repo`` (``ROOT/<repo>``); never falls back to a download."""
    if (overrides or {}).get(repo):
        p = Path(overrides[repo]).expanduser()
        if not p.is_dir():
            raise PlasticError(f"local folder for {repo} not found: {p}")
        return p
    if not root:
        raise PlasticError(f"no local folder given for {repo}")
    cand = Path(root).expanduser() / repo
    if cand.is_dir() and any(cand.iterdir()):
        return cand
    raise PlasticError(f"no local folder for {repo}; looked in: {cand}")


def tree_stats(tree: Path) -> tuple[int, int]:
    """``(files, bytes)`` on disk, ignoring Plastic's own metadata (``.plastic``, ``.plastic4``, ``ignore.conf``)."""
    files = size = 0
    for root, dirs, names in os.walk(tree):
        dirs[:] = [d for d in dirs if not d.startswith(".plastic")]
        for name in names:
            if name.startswith(".plastic") or name == "ignore.conf":
                continue
            try:
                size += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                continue
            files += 1
    return files, size


def sync_working_tree(
    conn: PlasticConn, repo: str, dest: Path, log: logging.Logger | None = None, timeout: int = 3600
) -> int:
    """Download the main branch head into ``dest`` through a throw-away workspace; returns the file count.

    ``dest`` is scratch (the caller deletes it). The workspace registration is always removed."""
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True)
    wk = f"datalabs-{uuid.uuid4().hex[:12]}"
    try:
        branch = main_branch(conn, repo)
        if log is not None:
            log.info("plastic %s: downloading %s head into %s (large repositories take a while; "
                     "use a local workspace folder to skip it)", repo, branch, dest)
        run_cm(conn, "workspace", "create", wk, str(dest), f"--repository={conn.spec(repo)}", timeout=120)
        run_cm(conn, "switch", f"br:{branch}", f"--workspace={dest}", timeout=timeout, check=False)
        run_cm(conn, "update", str(dest), timeout=timeout)
        files, _ = tree_stats(dest)
        if log is not None:
            log.info("synced %s files of %s into %s", files, repo, dest)
        return files
    finally:
        run_cm(conn, "workspace", "delete", wk, check=False, timeout=60)
