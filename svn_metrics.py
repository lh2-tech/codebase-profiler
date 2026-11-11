"""Subversion history metrics, shaped like the Git ones in extract_org_raw_data.

A target is one repository URL (``https://host/svn/repo/trunk``, ``svn://…``, ``file:///…``), read with
the ``svn`` CLI. Like the Perforce and Plastic modules it returns the structures the Git path
produces (``git_stats.json``, ``commits.jsonl``) so the summary columns and archive are identical
for every VCS. Only change metadata and line counts are returned, never file contents.

Credentials. The password is only ever given to ``svn`` on stdin (``--password-from-stdin``) with
``--no-auth-cache``, so it is not in the process list, not stored by svn, and not in the log. A URL
carrying ``user:pass@`` is refused. A server with an untrusted certificate fails with a message that
says so; set ``SVN_TRUST_SERVER_CERT=1`` only after verifying the server yourself.

Scope. "Commits" are the revisions that touch the URL (paths outside it are ignored). Subversion has
no pull requests: a revision that *copies a directory* into the scope (branch/tag creation) or changes
``svn:mergeinfo`` is the equivalent of a Git merge commit, so those are what ``merged_prs`` counts and
they are written without a file list, so a branch copy does not look like thousands of authored lines.

Line counts. ``svn diff -c N`` is a unified diff of the whole revision, added and deleted files
included, so no per-file calls are needed. Past ``max_commits`` only the newest revisions are diffed;
older ones are still scanned for directory copies, but a mergeinfo-only merge among them is not seen.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import unquote, urlsplit

DIFF_WORKERS = 6
MERGEINFO = "svn:mergeinfo"


class SvnError(RuntimeError):
    """An svn command failed; the message is safe to show (no credentials in it)."""


@dataclass
class SvnConn:
    """Credentials for one repository host. Empty user/password means: anonymous or the user's own svn config."""

    user: str = ""
    password: str = field(default="", repr=False)
    trust_cert: bool = False

    def base(self) -> list[str]:
        args = ["--non-interactive", "--no-auth-cache"]
        if self.user:
            args += ["--username", self.user]
        if self.password:
            args.append("--password-from-stdin")
        if self.trust_cert:
            args.append("--trust-server-cert-failures=unknown-ca,cn-mismatch,expired,not-yet-valid,other")
        return args


def svn_available() -> bool:
    return shutil.which("svn") is not None


def normalize_url(url: str) -> str:
    """Validate and canonicalise a repository URL (no trailing slash, no embedded credentials)."""
    u = url.strip().rstrip("/")
    parts = urlsplit(u)
    if parts.scheme not in {"http", "https", "svn", "svn+ssh", "file"}:
        raise ValueError(f"not a Subversion URL (http, https, svn, svn+ssh or file): {url!r}")
    if parts.password is not None or (parts.username and parts.scheme != "svn+ssh"):
        raise ValueError("put the user name in --svn-user and the password in SVN_PASSWORD, not in the URL")
    return u


def host_of(url: str) -> str:
    return urlsplit(url).hostname or "local"


def slug_for(url: str) -> str:
    path = unquote(urlsplit(url).path).strip("/")
    return re.sub(r"[^A-Za-z0-9._-]+", "-", path.replace("/", "-")).strip("-") or "repo"


def _scrub(conn: SvnConn, text: str) -> str:
    return text.replace(conn.password, "***") if conn.password else text


def run_svn(conn: SvnConn, *args: str, timeout: int = 300, check: bool = True, binary: bool = False) -> Any:
    proc = subprocess.run(
        ["svn", *args, *conn.base()],
        input=(conn.password + "\n").encode() if conn.password else None,
        capture_output=True, timeout=timeout, env={**os.environ, "LC_ALL": "C"},
    )
    if proc.returncode != 0 and check:
        msg = _scrub(conn, (proc.stderr or proc.stdout).decode("utf-8", "replace").strip())
        if "certificate" in msg.lower():
            msg += " (verify the server, then set SVN_TRUST_SERVER_CERT=1 to accept it)"
        raise SvnError(f"svn {args[0] if args else ''} failed: {msg[:300]}")
    return proc.stdout if binary else proc.stdout.decode("utf-8", "replace")


def connect(conn: SvnConn, url: str) -> dict[str, str]:
    """Prove the URL is readable and return ``{root, relative, uuid, head}``."""
    if not svn_available():
        raise SvnError("`svn` is not installed (Subversion command-line client)")
    root = ET.fromstring(run_svn(conn, "info", "--xml", url, timeout=60)).find("entry")
    if root is None:
        raise SvnError(f"{url} is not a Subversion path")
    repo = root.find("repository")
    return {
        "root": (repo.findtext("root") if repo is not None else "") or "",
        "uuid": (repo.findtext("uuid") if repo is not None else "") or "",
        "head": root.get("revision", ""),
        "relative": unquote(root.findtext("relative-url") or ""),  # "^/trunk"
    }


def list_children(conn: SvnConn, url: str) -> list[str]:
    """Immediate sub-folders of ``url`` (trunk, branches, tags, or one folder per project)."""
    out = run_svn(conn, "list", url, timeout=120)
    return sorted(ln.rstrip("/") for ln in out.splitlines() if ln.endswith("/"))


# --------------------------------------------------------------------------- history


def _iso(value: str) -> str:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
    except ValueError:
        return ""


def iter_revisions(conn: SvnConn, url: str, info: dict[str, str], timeout: int = 900) -> Iterator[dict[str, Any]]:
    """Every revision touching ``url``, newest first, with its paths restricted to the scope."""
    scope = info["relative"][1:] if info["relative"].startswith("^") else info["relative"]  # "/trunk"
    prefix = scope.rstrip("/") + "/"
    out = run_svn(conn, "log", "--xml", "-v", "-r", "HEAD:1", url, timeout=timeout)
    for entry in ET.fromstring(out).iter("logentry"):
        paths = []
        for p in entry.iter("path"):
            path = unquote(p.text or "")
            if path != scope and not path.startswith(prefix):
                continue
            paths.append({
                "path": path[len(prefix):] if path.startswith(prefix) else "",
                "action": p.get("action", ""), "kind": p.get("kind", ""),
                "copyfrom": bool(p.get("copyfrom-path")),
            })
        msg = (entry.findtext("msg") or "").strip()
        yield {
            "change": int(entry.get("revision", "0")),
            "user": (entry.findtext("author") or "").strip(),  # absent for anonymous commits
            "date": _iso(entry.findtext("date") or ""),
            "desc": msg, "subject": msg.splitlines()[0] if msg else "",
            "paths": paths,
        }


def is_branch_copy(rev: dict[str, Any]) -> bool:
    """A directory added by copy: branch or tag creation."""
    return any(p["action"] == "A" and p["copyfrom"] and p["kind"] == "dir" for p in rev["paths"])


def aggregate_svn_stats(
    conn: SvnConn, url: str, info: dict[str, str], is_bot: Callable[[str, str], bool],
    log: logging.Logger | None = None, changes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Same keys as ``aggregate_git_stats`` plus Subversion-only extras."""
    total = 0
    first = last = None
    authors: dict[str, int] = {}
    for c in changes if changes is not None else iter_revisions(conn, url, info):
        total += 1
        name = c["user"] or "(unknown)"
        authors[name] = authors.get(name, 0) + 1
        ts = datetime.fromisoformat(c["date"]) if c["date"] else None
        if ts is None:
            continue
        first = ts if first is None or ts < first else first
        last = ts if last is None or ts > last else last
    rows = sorted(
        ({"name": n, "email": "", "commits": k, "is_bot": bool(is_bot(n, ""))} for n, k in authors.items()),
        key=lambda a: (-a["commits"], a["name"].lower()),
    )
    human = [a for a in rows if not a["is_bot"]]
    bots = [a for a in rows if a["is_bot"]]
    bot_commits = sum(a["commits"] for a in bots)
    return {
        "vcs": "svn",
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
        "commits_per_user": dict(sorted(authors.items(), key=lambda kv: -kv[1])[:100]),
        "url": url,
        "repository_uuid": info.get("uuid", ""),
        "head_revision": info.get("head", ""),
    }


# --------------------------------------------------------------------------- per-revision churn

_INDEX = re.compile(r"^Index: (.+)$")
_PROP = re.compile(r"^(?:Added|Modified|Deleted): (\S+)\s*$")


def parse_diff(text: str) -> tuple[dict[str, list[int]], bool]:
    """``({path: [additions, deletions]}, mergeinfo_changed)`` from ``svn diff -c N`` output.

    File hunks start at ``@@``; property sections (``Property changes on:``) are never counted as
    lines, only inspected for ``svn:mergeinfo``."""
    files: dict[str, list[int]] = {}
    path: str | None = None
    in_hunk = False
    in_props = False
    mergeinfo = False
    for line in text.splitlines():
        m = _INDEX.match(line)
        if m:
            path, in_hunk, in_props = m.group(1), False, False
            files.setdefault(path, [0, 0])
            continue
        if line.startswith("Property changes on:"):
            in_hunk, in_props = False, True
            continue
        if in_props:
            pm = _PROP.match(line)
            if pm and pm.group(1) == MERGEINFO:
                mergeinfo = True
            continue
        if path is None:
            continue
        if line.startswith("@@"):
            in_hunk = True
        elif in_hunk and line.startswith("+") and not line.startswith("+++"):
            files[path][0] += 1
        elif in_hunk and line.startswith("-") and not line.startswith("---"):
            files[path][1] += 1
    return files, mergeinfo


def export_commits_jsonl(
    conn: SvnConn, url: str, out_path: Path, *, changes: list[dict[str, Any]],
    is_bot: Callable[[str, str], bool], max_commits: int = 0, timeout: int = 1800,
    log: logging.Logger | None = None, write_json: Callable[[Path, Any], None] | None = None,
) -> tuple[int, list[dict[str, str]]]:
    """Per-revision file churn as JSONL (same record shape as the Git exporter).

    Returns ``(records written, merge revisions)``. ``changes`` is newest first."""
    capped = bool(max_commits and len(changes) > max_commits)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    failed = 0

    def diff_of(rev: dict[str, Any]) -> tuple[dict[str, list[int]], bool] | None:
        try:
            return parse_diff(run_svn(conn, "diff", "-c", str(rev["change"]), url, timeout=600))
        except (SvnError, subprocess.TimeoutExpired):
            return None

    merges: list[dict[str, str]] = []
    count = 0
    with out_path.open("w", encoding="utf-8") as fh, ThreadPoolExecutor(DIFF_WORKERS) as pool:
        futures = {}
        for i, c in enumerate(changes):
            if not is_branch_copy(c) and (not capped or i < max_commits):
                futures[c["change"]] = pool.submit(diff_of, c)
        for i, c in enumerate(changes):
            if time.monotonic() > deadline:
                raise subprocess.TimeoutExpired("svn diff", timeout)
            if log is not None and i and i % 500 == 0:
                log.info("svn %s: described %s of %s revisions", url, f"{i:,}", f"{len(changes):,}")
            name = c["user"] or "(unknown)"
            files: list[dict[str, Any]] = []
            merge = is_branch_copy(c)
            fut = futures.get(c["change"])
            if fut is not None:
                res = fut.result()
                if res is None:
                    failed += 1
                else:
                    by_file, mergeinfo = res
                    merge = merge or mergeinfo
                    if not merge:
                        files = sorted(
                            ({"path": p, "additions": a, "deletions": d} for p, (a, d) in by_file.items()),
                            key=lambda f: f["path"],
                        )
            if merge:
                merges.append({"sha": str(c["change"]), "date": c["date"], "author": name,
                               "subject": c["subject"], "kind": "merge"})
            fh.write(json.dumps({
                "sha": str(c["change"]), "author_name": name, "author_email": "", "author_date": c["date"],
                "subject": c["subject"], "is_bot": bool(is_bot(name, "")), "files": files,
                "additions": sum(f["additions"] for f in files), "deletions": sum(f["deletions"] for f in files),
            }, ensure_ascii=False) + "\n")
            count += 1

    meta = {
        "total_commits": len(changes),
        "detail_commits_exported": min(len(changes), max_commits) if capped else len(changes),
        "capped": capped, "detail_limit": max_commits or None, "revisions_diff_failed": failed,
        "note": (
            "Line counts come from `svn diff -c N` (added/deleted files included). A revision that copies a "
            "directory or changes svn:mergeinfo is a merge and carries no file list. Beyond the cap, only "
            "directory copies are recognised as merges."
        ),
    }
    if capped or failed:
        if log is not None:
            log.warning("commits.jsonl: %s", json.dumps(meta))
        if write_json is not None:
            write_json(out_path.parent / "commits_detail_meta.json", meta)
    return count, merges


# --------------------------------------------------------------------------- working tree


def resolve_local_tree(url: str, root: str | Path | None = None, overrides: dict[str, str] | None = None) -> Path:
    """The existing working copy for ``url`` (``ROOT/<slug>``); never falls back to a download."""
    if (overrides or {}).get(url):
        p = Path(overrides[url]).expanduser()
        if not p.is_dir():
            raise SvnError(f"local folder for {url} not found: {p}")
        return p
    if not root:
        raise SvnError(f"no local folder given for {url}")
    cand = Path(root).expanduser() / slug_for(url)
    if cand.is_dir() and any(cand.iterdir()):
        return cand
    raise SvnError(f"no local folder for {url}; looked in: {cand}")


def tree_stats(tree: Path) -> tuple[int, int]:
    """``(files, bytes)`` on disk, ignoring the ``.svn`` metadata folder."""
    files = size = 0
    for root, dirs, names in os.walk(tree):
        dirs[:] = [d for d in dirs if d != ".svn"]
        for name in names:
            try:
                size += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                continue
            files += 1
    return files, size


def workspace_check(conn: SvnConn, url: str, tree: Path) -> dict[str, Any]:
    """Compare a local working copy with the URL's newest revision, by revision number (no download).

    A working copy behind head, or with local modifications, skews every number measured on it."""
    out: dict[str, Any] = {"tree": str(tree)}
    out["files_local"], out["bytes_local"] = tree_stats(tree)
    try:
        wc = ET.fromstring(run_svn(conn, "info", "--xml", str(tree), timeout=60)).find("entry")
        head = ET.fromstring(run_svn(conn, "info", "--xml", url, timeout=60)).find("entry")
        wc_rev = int(wc.find("commit").get("revision"))  # type: ignore[union-attr]
        head_rev = int(head.find("commit").get("revision"))  # type: ignore[union-attr]
        status = run_svn(conn, "status", "-q", str(tree), timeout=300)
    except (SvnError, AttributeError, TypeError, ValueError, subprocess.TimeoutExpired):
        return {**out, "checked": False, "note": "not a working copy of this URL, or the server was unreachable"}
    modified = len([ln for ln in status.splitlines() if ln.strip()])
    out.update(checked=True, working_copy_revision=wc_rev, head_revision=head_rev, modified_items=modified,
               within_tolerance=wc_rev >= head_rev and modified == 0)
    return out


def export_head(conn: SvnConn, url: str, dest: Path, log: logging.Logger | None = None, timeout: int = 3600) -> int:
    """Export the head revision into ``dest`` (no ``.svn`` metadata); returns the file count. ``dest`` is scratch."""
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    if log is not None:
        log.info("svn %s: exporting the head revision into %s (large repositories take a while; "
                 "use a local working copy to skip it)", url, dest)
    run_svn(conn, "export", "-q", "--force", url, str(dest), timeout=timeout)
    files, _ = tree_stats(dest)
    if log is not None:
        log.info("exported %s files of %s into %s", files, url, dest)
    return files
