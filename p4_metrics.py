"""Perforce (Helix Core) history metrics, shaped like the Git ones in extract_org_raw_data.

A Perforce depot lives on a server, so every call here goes through the ``p4`` CLI with an
explicit connection (``P4Conn``). Credentials are only ever passed in the environment, never on
a command line, so they cannot show up in the process list or in the run log. The module returns
the same structures the Git path produces (``git_stats.json``, ``commits.jsonl``), so the summary
columns and the output archive are identical for every VCS. Only change metadata and line counts
are returned — never file contents.

Scope. A target is one depot path (``//depot/project/...``). "Commits" are the submitted
changelists that touch it, and a changelist's files are restricted to that path (a changelist
that also touches another project counts only for the part inside this one). Perforce has no
pull requests: an *integration* changelist (any file with action ``integrate`` or ``branch``) is
the equivalent of a Git merge commit, so such changelists are what ``merged_prs`` counts and, as
with a Git merge, they are written without a file list so a branch copy does not look like
thousands of authored lines.

Line counts. ``p4 describe -du`` gives unified diffs for edited files only: added and deleted
files have no diff. Their line count comes from ``p4 print`` for text files (binary files count
0/0, as ``git log --numstat`` does). Because that is one call per file, it is bounded by
``P4_PRINT_LIMIT`` files and ``P4_PRINT_MAX_BYTES`` per file; anything beyond is left at 0 and
reported in ``commits_detail_meta.json``.
"""
from __future__ import annotations

import json
import logging
import marshal
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

# Changelists per ``p4 describe`` call; large enough to amortise the round trip, small enough
# that one huge changelist does not stall the stream.
DESCRIBE_BATCH = 40
P4_PRINT_LIMIT = int(os.environ.get("P4_PRINT_LIMIT", "50000"))
P4_PRINT_MAX_BYTES = int(os.environ.get("P4_PRINT_MAX_BYTES", str(2 * 1024 * 1024)))
P4_PRINT_WORKERS = 8
INTEGRATION_ACTIONS = {"integrate", "branch"}
# Actions that change no authored lines (a rename keeps its content).
NO_LINE_ACTIONS = {"move/add", "move/delete", "purge", "archive", "import"}


class P4Error(RuntimeError):
    """A p4 command failed; the message is safe to show (no credentials in it)."""


@dataclass
class P4Conn:
    """How to reach one Perforce server. ``password`` may be a password or a login ticket."""

    port: str
    user: str
    password: str = field(default="", repr=False)
    charset: str = ""  # set by ``connect`` from the server's unicode mode
    ticket: str = field(default="", repr=False)

    def env(self) -> dict[str, str]:
        env = {
            **os.environ,
            "P4PORT": self.port,
            "P4USER": self.user,
            # Ignore the caller's p4 config files so a stray P4CONFIG / P4CLIENT cannot redirect a run.
            "P4CONFIG": "",
            "P4ENVIRO": os.devnull,
            "P4CLIENT": "",
        }
        secret = self.ticket or self.password
        if secret:
            env["P4PASSWD"] = secret
        else:
            env.pop("P4PASSWD", None)
        if self.charset:
            env["P4CHARSET"] = self.charset
        return env

    def label(self) -> str:
        return f"{self.user}@{self.port}"


def p4_available() -> bool:
    return shutil.which("p4") is not None


def normalize_depot_path(path: str) -> str:
    """``//depot/proj``, ``//depot/proj/`` and ``//depot/proj/...`` all become ``//depot/proj/...``."""
    p = path.strip()
    if not p.startswith("//"):
        raise ValueError(f"not a depot path (must start with //): {path!r}")
    p = p.rstrip("/")
    if p.endswith("..."):
        p = p[:-3].rstrip("/")
    return p + "/..."


def path_prefix(depot_path: str) -> str:
    """``//depot/proj/...`` -> ``//depot/proj/`` (what every file in scope starts with)."""
    return normalize_depot_path(depot_path)[:-3]


def slug_for(depot_path: str) -> str:
    """``//depot/a/b/...`` -> ``depot/a/b`` (used for the repo name in summary.csv)."""
    return path_prefix(depot_path)[2:].rstrip("/")


# --------------------------------------------------------------------------- running p4


def _cmd(conn: P4Conn, *args: str, client: str = "") -> list[str]:
    base = ["p4"]
    if client:
        base += ["-c", client]
    return [*base, *args]


def run_p4_text(
    conn: P4Conn,
    *args: str,
    client: str = "",
    timeout: int = 300,
    stdin: str | None = None,
    check: bool = True,
) -> str:
    proc = subprocess.run(
        _cmd(conn, *args, client=client),
        input=stdin,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
        env=conn.env(),
    )
    if proc.returncode != 0 and check:
        raise P4Error(f"p4 {args[0] if args else ''} failed: {_scrub(conn, (proc.stderr or proc.stdout).strip())[:300]}")
    return proc.stdout


def _scrub(conn: P4Conn, text: str) -> str:
    for secret in (conn.password, conn.ticket):
        if secret:
            text = text.replace(secret, "***")
    return text


def _dec(value: Any) -> Any:
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else value


def iter_marshal(
    conn: P4Conn,
    *args: str,
    client: str = "",
    timeout: int = 600,
    log: logging.Logger | None = None,
) -> Iterator[dict[str, str]]:
    """Run ``p4 -G …`` and yield one decoded dict per record; raise P4Error on an error record."""
    cmd = ["p4", "-G", *(["-c", client] if client else []), *args]
    with tempfile.TemporaryFile("w+", errors="replace") as err_f:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err_f, env=conn.env())
        deadline = time.monotonic() + timeout
        assert proc.stdout is not None
        seen = 0
        # The deadline below is only checked when a record arrives; `sync -q` and a hung connect produce none,
        # so a watchdog kills the process at the deadline regardless.
        timed_out = threading.Event()

        def _expire() -> None:
            timed_out.set()
            if proc.poll() is None:
                proc.kill()

        watchdog = threading.Timer(timeout, _expire)
        watchdog.daemon = True
        watchdog.start()
        try:
            while True:
                try:
                    rec = marshal.load(proc.stdout)
                except EOFError:
                    break
                seen += 1
                row = {_dec(k): _dec(v) for k, v in rec.items()}
                if row.get("code") == "error":
                    # Severity 1-2 are informational ("no such file(s)"); 3+ are real failures.
                    if int(row.get("severity", "3")) >= 3:
                        raise P4Error(_scrub(conn, str(row.get("data", "p4 error")).strip())[:300])
                    continue
                yield row
                if time.monotonic() > deadline:
                    proc.kill()
                    raise subprocess.TimeoutExpired(cmd, timeout)
            proc.wait()
            if timed_out.is_set():
                raise subprocess.TimeoutExpired(cmd, timeout)
            if proc.returncode != 0 and seen == 0:
                # Nothing marshalled at all: the connection itself failed (refused, SSL, DNS…).
                err_f.seek(0)
                raise P4Error(_scrub(conn, err_f.read().strip() or f"p4 exited {proc.returncode}")[:300])
        finally:
            watchdog.cancel()
            if proc.poll() is None:
                proc.kill()
            proc.stdout.close()
            proc.wait()
            if proc.returncode not in (0, None) and seen and log is not None and not timed_out.is_set():
                err_f.seek(0)
                log.warning("p4 exited %s: %s", proc.returncode, _scrub(conn, err_f.read().strip())[:300])


def run_marshal(conn: P4Conn, *args: str, **kw: Any) -> list[dict[str, str]]:
    return list(iter_marshal(conn, *args, **kw))


# --------------------------------------------------------------------------- connect


def connect(conn: P4Conn, log: logging.Logger | None = None, fingerprint: str = "") -> dict[str, str]:
    """Establish the session and return ``p4 info``. Raises P4Error with a usable message.

    * ``fingerprint`` trusts an SSL server whose fingerprint the user supplied. A fingerprint
      is never accepted automatically: that would defeat the point of ``p4 trust``.
    * With a password, ``p4 login -p`` turns it into a ticket (the password goes in on stdin),
      so a server at security level 3 works too. If there is no password set on the account the
      login fails harmlessly and commands run as-is.
    """
    if not p4_available():
        raise P4Error("`p4` is not installed (see the Perforce 'Mercurial-style' setup in the README)")
    if conn.port.startswith("ssl:") and fingerprint:
        run_p4_text(conn, "trust", "-i", fingerprint, check=False)
    try:
        info = _first(run_marshal(conn, "info", timeout=30))
    except P4Error as exc:
        if "trust" in str(exc).lower():
            raise P4Error(
                f"{conn.port} uses SSL and its fingerprint is not trusted yet. Verify it with your "
                "Perforce admin and pass it as the fingerprint (or run `p4 trust` once)."
            ) from exc
        raise
    if info.get("unicode") == "enabled" and not conn.charset:
        conn.charset = "utf8"
    if conn.password:
        out = run_p4_text(conn, "login", "-p", stdin=conn.password + "\n", check=False, timeout=30)
        lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
        if lines and re.fullmatch(r"[0-9A-Fa-f]{32}", lines[-1]):
            conn.ticket = lines[-1]
    # A read that needs auth proves the credentials work (``p4 info`` often does not).
    run_marshal(conn, "depots", timeout=60)
    return info


def _first(rows: list[dict[str, str]]) -> dict[str, str]:
    return rows[0] if rows else {}


def list_depots(conn: P4Conn) -> list[dict[str, str]]:
    return [
        {"name": r.get("name", ""), "type": r.get("type", ""), "desc": (r.get("desc") or "").strip()}
        for r in iter_marshal(conn, "depots")
        if r.get("name")
    ]


def list_top_dirs(conn: P4Conn, depot: str) -> list[str]:
    """Immediate sub-directories of ``//depot`` — the usual "repository" boundary."""
    return sorted(r["dir"] for r in iter_marshal(conn, "dirs", f"//{depot}/*") if r.get("dir"))


# --------------------------------------------------------------------------- history


def _iso(epoch: str | int) -> str:
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc).isoformat()


def user_directory(conn: P4Conn) -> dict[str, tuple[str, str]]:
    """``{user: (full name, email)}`` from ``p4 users``; email is "" when it is not an address."""
    out: dict[str, tuple[str, str]] = {}
    for r in iter_marshal(conn, "users"):
        name = (r.get("FullName") or "").strip() or r.get("User", "")
        email = (r.get("Email") or "").strip()
        out[r.get("User", "")] = (name, email if "@" in email else "")
    return out


def iter_changes(
    conn: P4Conn,
    depot_path: str,
    *,
    timeout: int = 900,
    log: logging.Logger | None = None,
) -> Iterator[dict[str, Any]]:
    """Every submitted changelist that touches ``depot_path``, newest first."""
    for r in iter_marshal(
        conn, "changes", "-t", "-l", "-s", "submitted", normalize_depot_path(depot_path),
        timeout=timeout, log=log,
    ):
        if "change" not in r:
            continue
        desc = (r.get("desc") or "").strip()
        yield {
            "change": int(r["change"]),
            "time": int(r.get("time", "0") or 0),
            "user": r.get("user", ""),
            "client": r.get("client", ""),
            "desc": desc,
            "subject": desc.splitlines()[0] if desc else "",
        }


def all_change_ids(conn: P4Conn, depot_path: str, timeout: int = 900) -> list[int]:
    return [c["change"] for c in iter_changes(conn, depot_path, timeout=timeout)]


def _parse_date_epoch(ts: int) -> datetime | None:
    # Epoch-0 is a placeholder some migrated depots carry (see the Mercurial note); skip it.
    return datetime.fromtimestamp(ts, tz=timezone.utc) if ts > 0 else None


def aggregate_p4_stats(
    conn: P4Conn,
    depot_path: str,
    is_bot: Callable[[str, str], bool],
    log: logging.Logger | None = None,
    changes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Same keys as ``aggregate_git_stats`` plus Perforce-only extras."""
    users = user_directory(conn)
    total = 0
    first: datetime | None = None
    last: datetime | None = None
    authors: dict[tuple[str, str], int] = {}
    per_user: dict[str, int] = {}
    for c in changes if changes is not None else iter_changes(conn, depot_path, log=log):
        total += 1
        name, email = users.get(c["user"], (c["user"], ""))
        key = (name, email)
        authors[key] = authors.get(key, 0) + 1
        per_user[c["user"]] = per_user.get(c["user"], 0) + 1
        ts = _parse_date_epoch(c["time"])
        if ts is None:
            continue
        if first is None or ts < first:
            first = ts
        if last is None or ts > last:
            last = ts

    author_rows = sorted(
        (
            {"name": name, "email": email, "commits": count, "is_bot": bool(is_bot(name, email))}
            for (name, email), count in authors.items()
        ),
        key=lambda a: (-a["commits"], a["name"].lower()),
    )
    human = [a for a in author_rows if not a["is_bot"]]
    bots = [a for a in author_rows if a["is_bot"]]
    bot_commits = sum(a["commits"] for a in bots)
    recency_days = span_days = None
    if last is not None:
        recency_days = (datetime.now(timezone.utc) - last).days
    if first is not None and last is not None:
        span_days = (last - first).days
    return {
        "vcs": "p4",
        "total_commits": total,
        "first_commit": first.isoformat() if first else None,
        "last_commit": last.isoformat() if last else None,
        "span_days": span_days,
        "recency_days": recency_days,
        "human_authors": len(human),
        "bot_authors": len(bots),
        "bot_commit_count": bot_commits,
        "bot_commit_ratio": round(bot_commits / total, 4) if total else 0.0,
        "authors": author_rows[:50],
        "commits_per_user": dict(sorted(per_user.items(), key=lambda kv: -kv[1])[:100]),
        **p4_repo_extras(conn, depot_path, log),
    }


def p4_repo_extras(conn: P4Conn, depot_path: str, log: logging.Logger | None = None) -> dict[str, Any]:
    """Server / scope inventory: counts and names only, never content."""
    scope = normalize_depot_path(depot_path)
    extras: dict[str, Any] = {"depot_path": scope, "server": conn.port}
    try:
        info = _first(run_marshal(conn, "info", timeout=30))
        extras["server_version"] = info.get("serverVersion", "")
        extras["case_handling"] = info.get("caseHandling", "")
        extras["unicode"] = info.get("unicode", "disabled")
    except P4Error:
        pass
    for key, args in (
        ("pending_changelists", ("changes", "-s", "pending", scope)),
        ("shelved_changelists", ("changes", "-s", "shelved", scope)),
    ):
        try:
            extras[key] = len(run_marshal(conn, *args, timeout=120))
        except P4Error:
            extras[key] = None  # no permission / not supported: unknown, not zero
    try:
        extras["stream_count"] = len(run_marshal(conn, "streams", "-F", f"Stream={scope.replace('/...', '/*')}", timeout=60))
    except P4Error:
        extras["stream_count"] = None
    try:
        extras["label_count"] = len(run_marshal(conn, "labels", "-m", "1000", timeout=60))
    except P4Error:
        extras["label_count"] = None
    return extras


# --------------------------------------------------------------------------- describe parsing

_HEADER = re.compile(r"^Change (\d+) by (.+?)@(\S+) on (\d{4}/\d\d/\d\d \d\d:\d\d:\d\d)")
_AFFECTED = re.compile(r"^\.\.\. (.+)#(\d+) (\S+)$")
_FILE_HDR = re.compile(r"^==== (.+)#(\d+) \((\S+)\) ====\s*$")


def parse_describe(text: str) -> dict[int, dict[str, Any]]:
    """Parse ``p4 describe -s|-du`` text into ``{change: {files: [...], diffs: {path: (adds, dels)}}}``."""
    out: dict[int, dict[str, Any]] = {}
    cur: dict[str, Any] | None = None
    path: str | None = None
    in_hunk = False
    for line in text.splitlines():
        m = _HEADER.match(line)
        if m:
            cur = {"files": [], "diffs": {}}
            out[int(m.group(1))] = cur
            path, in_hunk = None, False
            continue
        if cur is None:
            continue
        a = _AFFECTED.match(line)
        if a and path is None:
            cur["files"].append({"path": a.group(1), "rev": int(a.group(2)), "action": a.group(3)})
            continue
        h = _FILE_HDR.match(line)
        if h:
            path, in_hunk = h.group(1), False
            cur["diffs"].setdefault(path, [0, 0])
            continue
        if path is None:
            continue
        if line.startswith("@@"):
            in_hunk = True
        elif in_hunk and line[:1] == "+":
            cur["diffs"][path][0] += 1
        elif in_hunk and line[:1] == "-":
            cur["diffs"][path][1] += 1
    return out


def _is_text_type(ftype: str) -> bool:
    base = ftype.split("+", 1)[0]
    return base in {"text", "unicode", "utf8", "utf16", "xtext", "ctext", "ktext"}


def export_commits_jsonl(
    conn: P4Conn,
    depot_path: str,
    out_path: Path,
    *,
    changes: list[dict[str, Any]],
    is_bot: Callable[[str, str], bool],
    max_commits: int = 0,
    timeout: int = 1800,
    log: logging.Logger | None = None,
    write_json: Callable[[Path, Any], None] | None = None,
) -> tuple[int, list[dict[str, str]]]:
    """Per-changelist file churn as JSONL (same record shape as the Git exporter).

    Returns ``(records written, integration changelists)``. ``changes`` is the newest-first list
    from ``iter_changes``. Past ``max_commits`` only the newest changelists get per-file churn;
    the rest are still described (``-s``) so integrations are detected across the whole history.
    """
    users = user_directory(conn)
    prefix = path_prefix(depot_path)
    scope_label = normalize_depot_path(depot_path)
    capped = bool(max_commits and len(changes) > max_commits)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    integrations: list[dict[str, str]] = []
    print_budget = {"left": P4_PRINT_LIMIT, "skipped_files": 0, "skipped_unreadable": 0}
    count = 0
    deadline = time.monotonic() + timeout

    # Latest revision of the text/binary type for each file we may need to print is learned from
    # ``p4 files``-style data in describe -s only for the head; instead we trust the file extension
    # fallback: print returns the bytes, and binary content is detected from NULs below.
    def is_binary(data: bytes) -> bool:
        return b"\x00" in data[:8000]

    def lines_of(file: dict[str, Any], action: str) -> tuple[int, int]:
        """(additions, deletions) for an added or deleted file."""
        if print_budget["left"] <= 0:
            print_budget["skipped_files"] += 1
            return 0, 0
        print_budget["left"] -= 1
        rev = file["rev"] if action == "add" else file["rev"] - 1
        if rev < 1:
            return 0, 0
        spec = f"{file['path']}#{rev}"
        try:
            proc = subprocess.run(["p4", "print", "-q", spec], capture_output=True, timeout=120, env=conn.env())
        except subprocess.TimeoutExpired:
            print_budget["skipped_unreadable"] += 1
            return 0, 0
        data = proc.stdout
        if proc.returncode != 0 or len(data) > P4_PRINT_MAX_BYTES:
            print_budget["skipped_unreadable"] += 1
            return 0, 0
        if not data or is_binary(data):
            return 0, 0
        n = data.count(b"\n") + (0 if data.endswith(b"\n") else 1)
        return (n, 0) if action == "add" else (0, n)

    with out_path.open("w", encoding="utf-8") as fh, ThreadPoolExecutor(P4_PRINT_WORKERS) as pool:
        for start in range(0, len(changes), DESCRIBE_BATCH):
            if time.monotonic() > deadline:
                raise subprocess.TimeoutExpired("p4 describe", timeout)
            batch = changes[start : start + DESCRIBE_BATCH]
            if log is not None and start and (start // DESCRIBE_BATCH) % 25 == 0:
                log.info("p4 %s: described %s of %s changelists", scope_label, f"{start:,}", f"{len(changes):,}")
            detailed = not capped or start < max_commits
            flag = "-du" if detailed else "-s"
            text = run_p4_text(
                conn, "describe", flag, *[str(c["change"]) for c in batch], timeout=600, check=False
            )
            parsed = parse_describe(text)
            for c in batch:
                info = parsed.get(c["change"], {"files": [], "diffs": {}})
                scoped = [f for f in info["files"] if f["path"].startswith(prefix)]
                is_merge = any(f["action"] in INTEGRATION_ACTIONS for f in scoped)
                name, email = users.get(c["user"], (c["user"], ""))
                if is_merge:
                    integrations.append(
                        {
                            "sha": str(c["change"]),
                            "date": _iso(c["time"]),
                            "author": name,
                            "subject": c["subject"],
                            "kind": "integration",
                        }
                    )
                files: list[dict[str, Any]] = []
                if detailed and not is_merge and (not capped or start + batch.index(c) < max_commits):
                    pending: list[tuple[dict[str, Any], Any]] = []
                    for f in scoped:
                        act = f["action"]
                        if act in ("add", "delete"):
                            pending.append((f, pool.submit(lines_of, f, act)))
                        elif act in NO_LINE_ACTIONS:
                            files.append({"path": f["path"][len(prefix):], "additions": 0, "deletions": 0})
                        else:
                            add, dele = info["diffs"].get(f["path"], (0, 0))
                            files.append({"path": f["path"][len(prefix):], "additions": add, "deletions": dele})
                    for f, fut in pending:
                        add, dele = fut.result()
                        files.append({"path": f["path"][len(prefix):], "additions": add, "deletions": dele})
                    files.sort(key=lambda x: x["path"])
                rec = {
                    "sha": str(c["change"]),
                    "author_name": name,
                    "author_email": email,
                    "author_date": _iso(c["time"]) if c["time"] > 0 else "",
                    "subject": c["subject"],
                    "is_bot": bool(is_bot(name, email)),
                    "files": files,
                    "additions": sum(f["additions"] for f in files),
                    "deletions": sum(f["deletions"] for f in files),
                }
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                count += 1

    meta = {
        "total_commits": len(changes),
        "detail_commits_exported": min(len(changes), max_commits) if capped else len(changes),
        "capped": capped,
        "detail_limit": max_commits or None,
        "files_over_print_budget": print_budget["skipped_files"],
        "files_unreadable_or_too_large": print_budget["skipped_unreadable"],
        "note": (
            "Perforce describe has no line counts for added/deleted files; those are counted with "
            "p4 print for text files, bounded by P4_PRINT_LIMIT / P4_PRINT_MAX_BYTES. Integration "
            "changelists carry no file list (like a Git merge)."
        ),
    }
    if capped or print_budget["skipped_files"] or print_budget["skipped_unreadable"]:
        if log is not None:
            log.warning("commits.jsonl: %s", json.dumps(meta))
        if write_json is not None:
            write_json(out_path.parent / "commits_detail_meta.json", meta)
    return count, integrations


def branch_origin(
    conn: P4Conn,
    depot_path: str,
    tree_files: int,
    log: logging.Logger | None = None,
    limit: int = 300000,
) -> dict[str, Any]:
    """How much of this path is a branch of somewhere else, from Perforce's own integration records.

    A path created by ``p4 integ`` from another path is a copy: its LOC and history are not independent
    work. ``branch_copy_pct`` is the share of the synced files that have an integration record from
    *outside* this path (capped at 100: deleted files are in the records but not in the tree).
    Integrations from inside the same path are ordinary merges and ignored. Reading stops after
    ``limit`` records on a huge depot and says so.
    """
    prefix = path_prefix(depot_path)
    scope = normalize_depot_path(depot_path)
    into: set[str] = set()
    sources: dict[str, int] = {}
    n = 0
    truncated = False
    try:
        for r in iter_marshal(conn, "integrated", scope, log=log, timeout=900):
            n += 1
            if n > limit:
                truncated = True
                break
            to, frm = r.get("toFile", ""), r.get("fromFile", "")
            # `p4 integrated` also lists the mirror record where this path is the *source* ("branch into",
            # "merge into"); only inbound ones ("branch from", "merge from", …) say where this path came from.
            if not r.get("how", "").endswith(" from") or not to.startswith(prefix) or not frm or frm.startswith(prefix):
                continue
            into.add(to)
            root = "/".join(frm.split("/")[:4])  # //depot/<a>
            sources[root] = sources.get(root, 0) + 1
    except P4Error:
        return {"integration_sources": [], "branch_copy_pct": None, "notes": "p4 integrated unreadable"}
    top = sorted(sources.items(), key=lambda kv: -kv[1])[:5]
    return {
        "integrated_files": len(into),
        "branch_copy_pct": round(min(100.0, 100 * len(into) / tree_files), 1) if tree_files else None,
        "integration_sources": [f"{path} ({count})" for path, count in top],
        "notes": f"integration records read up to {limit}" if truncated else "",
    }


# --------------------------------------------------------------------------- working tree


def _force_rmtree(path: Path) -> None:
    """``p4 sync`` leaves files read-only unless the client allows writes; remove regardless."""

    def onerror(func: Callable[..., Any], p: str, _exc: Any) -> None:
        os.chmod(p, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
        func(p)

    shutil.rmtree(path, onerror=onerror)


def head_size(conn: P4Conn, depot_path: str) -> tuple[int, int] | None:
    """``(files, bytes)`` of the head revision of the path (deleted files excluded), or None if unreadable.

    One cheap server-side summary: it is how a run learns, before downloading anything, how big the
    download will be."""
    try:
        for r in iter_marshal(conn, "sizes", "-s", normalize_depot_path(depot_path), timeout=300):
            return int(r.get("fileCount", 0)), int(r.get("fileSize", 0))
    except (P4Error, subprocess.TimeoutExpired):
        return None
    return 0, 0


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


def resolve_local_tree(
    depot_path: str,
    root: str | Path | None = None,
    overrides: dict[str, str] | None = None,
) -> Path:
    """The folder that already holds this depot path on this machine (a synced workspace), so nothing is downloaded.

    ``overrides`` maps a depot path to its folder exactly. Otherwise ``root`` is a workspace root and the folder
    is looked up the usual ways: ``ROOT/<path under the depot>`` (workspace maps ``//depot/...`` to the root) or
    ``ROOT/<depot>/<path under the depot>``. Raises P4Error, naming every place tried, when nothing is found —
    a run that asked for a local tree must never silently fall back to a large download.
    """
    scope = normalize_depot_path(depot_path)
    for key, folder in (overrides or {}).items():
        if normalize_depot_path(key) == scope:
            p = Path(folder).expanduser()
            if not p.is_dir():
                raise P4Error(f"local folder for {scope} not found: {p}")
            return p
    if not root:
        raise P4Error(f"no local folder given for {scope}")
    parts = path_prefix(scope)[2:].rstrip("/").split("/")
    depot, rest = parts[0], parts[1:]
    base = Path(root).expanduser()
    tried: list[Path] = []
    candidates = [base.joinpath(*rest), base.joinpath(depot, *rest)]  # a whole depot (no rest) tries ROOT, then ROOT/<depot>
    for cand in candidates:
        tried.append(cand)
        if cand.is_dir() and any(cand.iterdir()):
            return cand
    raise P4Error(f"no local folder for {scope}; looked in: " + ", ".join(str(t) for t in tried))


def tree_stats(tree: Path) -> tuple[int, int]:
    """``(files, bytes)`` on disk, ignoring Perforce's own dotfiles (``.p4config``, ``.p4ignore``)."""
    files = size = 0
    for root, dirs, names in os.walk(tree):
        dirs[:] = [d for d in dirs if not d.startswith(".p4")]
        for name in names:
            if name.startswith(".p4"):
                continue
            try:
                size += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                continue
            files += 1
    return files, size


def workspace_check(conn: P4Conn, depot_path: str, tree: Path, tolerance_pct: float = 2.0) -> dict[str, Any]:
    """Compare a local folder with the depot head by file count and bytes (metadata only, no download).

    A synced workspace can be behind head, or carry build output that was never submitted; either skews
    every number measured on it, so the difference is reported (and logged) instead of assumed away."""
    server = head_size(conn, depot_path)
    files, size = tree_stats(tree)
    out: dict[str, Any] = {"tree": str(tree), "files_local": files, "bytes_local": size}
    if server is None:
        return {**out, "checked": False, "note": "server size unavailable"}

    def diff(a: int, b: int) -> float:
        return round(100 * abs(a - b) / max(a, b, 1), 1)

    out.update(
        checked=True, files_server=server[0], bytes_server=server[1],
        files_diff_pct=diff(files, server[0]), bytes_diff_pct=diff(size, server[1]),
    )
    out["within_tolerance"] = out["files_diff_pct"] <= tolerance_pct and out["bytes_diff_pct"] <= tolerance_pct
    return out


def sync_working_tree(
    conn: P4Conn,
    depot_path: str,
    dest: Path,
    log: logging.Logger | None = None,
    timeout: int = 3600,
    progress_every: float = 20.0,
) -> int:
    """Sync the head revision of ``depot_path`` into ``dest`` through a throw-away client.

    Returns the number of files synced. The temporary client is always deleted; ``dest`` is the
    caller's to delete (it is scratch, never the user's own workspace).
    """
    scope = normalize_depot_path(depot_path)
    if dest.exists():
        _force_rmtree(dest)
    dest.mkdir(parents=True)
    client = f"datalabs-{uuid.uuid4().hex[:12]}"
    spec = (
        f"Client: {client}\nOwner: {conn.user}\nRoot: {dest}\n"
        "Options: allwrite noclobber nocompress unlocked nomodtime normdir\n"
        "SubmitOptions: leaveunchanged\nLineEnd: local\n"
        f"View:\n\t\"{scope}\" \"//{client}/...\"\n"
    )
    stop = threading.Event()
    try:
        run_p4_text(conn, "client", "-i", stdin=spec, timeout=60)
        total = head_size(conn, scope)
        if log is not None:
            log.info(
                "p4 %s: downloading the head revision%s into %s (this can take a long time on a large depot; "
                "use a local workspace folder to skip it)", scope,
                f" — {total[0]:,} files, {human_bytes(total[1])}" if total else "", dest,
            )

        def report() -> None:
            started = time.monotonic()
            while not stop.wait(progress_every):
                done_files, done_bytes = tree_stats(dest)
                if log is not None:
                    of = f" of {total[0]:,} files / {human_bytes(total[1])}" if total else ""
                    log.info("p4 %s: synced %s files / %s%s after %ds", scope, f"{done_files:,}",
                             human_bytes(done_bytes), of, int(time.monotonic() - started))

        threading.Thread(target=report, daemon=True).start()
        for _ in iter_marshal(conn, "sync", "-q", f"{scope}#head", client=client, timeout=timeout, log=log):
            pass
        files = sum(len(fs) for _, _, fs in os.walk(dest))
        if log is not None:
            log.info("synced %s files of %s into %s", files, scope, dest)
        return files
    finally:
        stop.set()
        run_p4_text(conn, "client", "-d", "-f", client, check=False, timeout=60)
