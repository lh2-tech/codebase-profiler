"""Mercurial history metrics, shaped like the Git ones in extract_org_raw_data.

Everything here reads a local ``.hg`` repository through the ``hg`` CLI and
returns the same structures the Git path produces (``git_stats.json``,
``commits.jsonl``), so the summary columns and the output archive are identical
for both VCSs. Only aggregate / commit metadata is returned — no file contents.

History scope differs from Git on purpose. Git's ``rev-list HEAD`` follows one
branch; Mercurial keeps many named branches in one repository and a working
copy is usually *not* on the newest one, so totals here cover every visible
changeset (``all()``). The working tree (LOC, languages, tests…) is whatever is
checked out, or — when the repo was cloned with ``-U`` and has no working tree
— an archive of the newest open head, which is what ``prepare_working_tree``
returns.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

NULL_NODE = "0" * 40
REC = "\x1e"
SEP = "\x1f"

# HGPLAIN drops the user's aliases/defaults so output is stable; HGENCODING makes
# non-ASCII author names round-trip instead of becoming "?" under a C locale.
_HG_ENV = {"HGPLAIN": "1", "HGENCODING": "utf-8", "HGRCPATH": ""}
# Host-mounted repos are often owned by another UID; without this hg ignores the
# repo's own .hg/hgrc (extensions, paths) with a "not trusting file" warning.
_TRUST = ["--config", "trusted.users=*", "--config", "trusted.groups=*"]


def hg_available() -> bool:
    return shutil.which("hg") is not None


def is_hg_repo(path: Path) -> bool:
    return (path / ".hg").is_dir()


def _hg_cmd(repo: Path, *args: str) -> list[str]:
    return ["hg", *_TRUST, "-R", str(repo), *args]


def _env() -> dict[str, str]:
    return {**os.environ, **_HG_ENV}


def run_hg(
    repo: Path,
    *args: str,
    timeout: int = 300,
    log: logging.Logger | None = None,
) -> str:
    """Run ``hg -R repo …``; return stdout, or "" (with a warning) on failure."""
    proc = subprocess.run(
        _hg_cmd(repo, *args),
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
        env=_env(),
    )
    if proc.returncode != 0:
        if log is not None:
            detail = (proc.stderr or proc.stdout or "hg failed").strip()
            log.warning("hg %s failed in %s: %s", " ".join(args), repo, detail)
        return ""
    return proc.stdout


def probe_local_hg(repo: Path, log: logging.Logger) -> bool:
    """True when history is readable; logs a clear reason otherwise."""
    if not hg_available():
        log.warning("`hg` is not installed; Mercurial repositories cannot be read")
        return False
    out = run_hg(repo, "log", "-r", "tip", "-T", "{rev}", log=log)
    return out.strip().lstrip("-").isdigit()


def _stream(
    cmd: list[str], timeout: int, log: logging.Logger | None
) -> Iterator[str]:
    """Yield stdout in chunks while the command runs; kill it past ``timeout``."""
    # stderr goes to a temp file, not a pipe: an unread full pipe would stall hg.
    with tempfile.TemporaryFile("w+", errors="replace") as err_f:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=err_f,
            text=True,
            errors="replace",
            env=_env(),
        )
        deadline = time.monotonic() + timeout
        assert proc.stdout is not None
        try:
            while True:
                chunk = proc.stdout.read(1 << 16)
                if not chunk:
                    break
                yield chunk
                if time.monotonic() > deadline:
                    proc.kill()
                    raise subprocess.TimeoutExpired(cmd, timeout)
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.stdout.close()
            proc.wait()
            if proc.returncode != 0 and log is not None:
                err_f.seek(0)
                log.warning("hg exited %s: %s", proc.returncode, err_f.read().strip()[:300])


def _records(chunks: Iterator[str]) -> Iterator[str]:
    buf = ""
    for chunk in chunks:
        buf += chunk
        *done, buf = buf.split(REC)
        yield from done
    if buf.strip():
        yield buf


def _clean_email(value: str) -> str:
    return value if "@" in value else ""


def iter_commits(
    repo: Path,
    *,
    with_body: bool = False,
    timeout: int = 600,
    log: logging.Logger | None = None,
) -> Iterator[dict[str, Any]]:
    """Every visible changeset, newest first."""
    fields = [
        "{node}",
        "{date|rfc3339date}",
        "{author|person}",
        "{author|email}",
        "{branch}",
        "{p2node}",
        "{desc|firstline}",
    ]
    if with_body:
        fields.append("{desc}")
    template = SEP.join(fields) + REC
    cmd = _hg_cmd(repo, "log", "-r", "reverse(all())", "-T", template)
    for raw in _records(_stream(cmd, timeout, log)):
        parts = raw.strip("\n").split(SEP)
        if len(parts) < 7:
            continue
        node, date, name, email, branch, p2, subject = parts[:7]
        yield {
            "sha": node,
            "date": date,
            "name": name.strip(),
            "email": _clean_email(email.strip()),
            "branch": branch,
            "is_merge": p2 != NULL_NODE,
            "subject": subject,
            "body": parts[7] if with_body and len(parts) > 7 else "",
        }


def _parse_date(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def all_nodes(repo: Path, timeout: int = 600) -> list[str]:
    """Every visible changeset node id (40-hex). Raises RuntimeError if hg fails."""
    proc = subprocess.run(
        _hg_cmd(repo, "log", "-r", "all()", "-T", "{node}\n"),
        capture_output=True, text=True, errors="replace", timeout=timeout, env=_env(),
    )
    if proc.returncode != 0:
        raise RuntimeError(f"hg log failed: {(proc.stderr or proc.stdout).strip()[:300]}")
    return proc.stdout.split()


def current_branch(repo: Path) -> str:
    return run_hg(repo, "branch").strip()


def working_copy_is_empty(repo: Path) -> bool:
    """True for ``hg clone -U`` / ``hg update null`` repos with nothing checked out."""
    parent = run_hg(repo, "log", "-r", ".", "-T", "{rev}").strip()
    if parent in ("", "-1"):
        return True
    for entry in repo.iterdir():
        if entry.name != ".hg":
            return False
    return True


def newest_open_head(repo: Path) -> tuple[str, str]:
    """(node, branch) of the most recently committed non-closed head."""
    out = run_hg(
        repo,
        "log",
        "-r",
        "first(sort(head() and not closed(), -date))",
        "-T",
        "{node}" + SEP + "{branch}",
    ).strip()
    if not out:
        out = run_hg(repo, "log", "-r", "tip", "-T", "{node}" + SEP + "{branch}").strip()
    node, _, branch = out.partition(SEP)
    return node, branch


def prepare_working_tree(
    repo: Path, scratch: Path, log: logging.Logger | None = None
) -> tuple[Path, str, bool]:
    """Return ``(tree_to_measure, branch, is_scratch_copy)``.

    A repo with a checked-out working tree is measured in place, exactly as the
    Git path measures a local clone. One with no working tree is exported with
    ``hg archive`` into ``scratch``; the caller must delete it afterwards.
    """
    if not working_copy_is_empty(repo):
        return repo, current_branch(repo), False
    node, branch = newest_open_head(repo)
    if not node:
        return repo, "", False
    if scratch.exists():
        shutil.rmtree(scratch)
    run_hg(repo, "archive", "-r", node, "-t", "files", str(scratch), timeout=1800, log=log)
    (scratch / ".hg_archival.txt").unlink(missing_ok=True)
    if log is not None:
        log.info("no working tree in %s; archived head %s (%s)", repo, node[:12], branch)
    return scratch, branch, True


def aggregate_hg_stats(
    repo: Path,
    is_bot: Callable[[str, str], bool],
    log: logging.Logger | None = None,
) -> dict[str, Any]:
    """Same keys as ``aggregate_git_stats`` plus Mercurial-only extras."""
    total = 0
    first: datetime | None = None
    last: datetime | None = None
    first_s = last_s = ""
    merges = 0
    authors: dict[tuple[str, str], int] = {}
    per_branch: dict[str, int] = {}
    for c in iter_commits(repo, log=log):
        total += 1
        merges += c["is_merge"]
        key = (c["name"], c["email"])
        authors[key] = authors.get(key, 0) + 1
        per_branch[c["branch"]] = per_branch.get(c["branch"], 0) + 1
        dt = _parse_date(c["date"])
        if dt is None:
            continue
        ts = dt.astimezone(timezone.utc)
        if first is None or ts < first.astimezone(timezone.utc):
            first, first_s = dt, c["date"]
        if last is None or ts > last.astimezone(timezone.utc):
            last, last_s = dt, c["date"]

    author_rows = sorted(
        (
            {
                "name": name,
                "email": email,
                "commits": count,
                "is_bot": bool(is_bot(name, email)),
            }
            for (name, email), count in authors.items()
        ),
        key=lambda a: (-a["commits"], a["name"].lower()),
    )
    human = [a for a in author_rows if not a["is_bot"]]
    bots = [a for a in author_rows if a["is_bot"]]
    bot_commits = sum(a["commits"] for a in bots)

    recency_days = span_days = None
    if last is not None:
        recency_days = (datetime.now(timezone.utc) - last.astimezone(timezone.utc)).days
    if first is not None and last is not None:
        span_days = (last - first).days

    return {
        "vcs": "hg",
        "total_commits": total,
        "first_commit": first_s or None,
        "last_commit": last_s or None,
        "span_days": span_days,
        "recency_days": recency_days,
        "human_authors": len(human),
        "bot_authors": len(bots),
        "bot_commit_count": bot_commits,
        "bot_commit_ratio": round(bot_commits / total, 4) if total else 0.0,
        "authors": author_rows[:50],
        "merge_commits": merges,
        "commits_per_branch": dict(
            sorted(per_branch.items(), key=lambda kv: -kv[1])[:100]
        ),
        **hg_repo_extras(repo, log),
    }


def hg_repo_extras(repo: Path, log: logging.Logger | None = None) -> dict[str, Any]:
    """Branch / head / tag / bookmark inventory (counts and names, no content)."""

    def lines(*args: str) -> list[str]:
        return [x for x in run_hg(repo, *args, log=log).splitlines() if x.strip()]

    branches = lines("branches", "-c", "-T", "{branch}\t{if(closed, 'closed', 'open')}\n")
    heads = lines("heads", "-T", "{node|short}\t{branch}\n")
    tags = [t.split("\t")[0] for t in lines("tags", "-T", "{tag}\n") if t != "tip"]
    marks = lines("bookmarks", "-T", "{bookmark}\n")
    subrepo = (repo / ".hgsub").is_file()
    return {
        "branch_count": len(branches),
        "open_branch_count": sum(1 for b in branches if b.endswith("\topen")),
        "head_count": len(heads),
        "tag_count": len(tags),
        "bookmark_count": len([m for m in marks if "no bookmarks set" not in m]),
        "has_subrepos": subrepo,
        "branches": [b.split("\t")[0] for b in branches][:200],
    }


def _diff_files(lines: list[str]) -> list[dict[str, Any]]:
    """Per-file +/- counts from one changeset's ``--git`` patch lines."""
    files: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    in_hunk = False
    for line in lines:
        if line.startswith("diff --git "):
            path = line.rsplit(" b/", 1)[-1] if " b/" in line else line[11:]
            cur = {"path": path, "additions": 0, "deletions": 0}
            files.append(cur)
            in_hunk = False
        elif cur is None:
            continue
        elif line.startswith("@@"):
            in_hunk = True
        elif in_hunk and line[:1] == "+":
            cur["additions"] += 1
        elif in_hunk and line[:1] == "-":
            cur["deletions"] += 1
    return files


def export_commits_jsonl(
    repo: Path,
    out_path: Path,
    *,
    is_bot: Callable[[str, str], bool],
    total_commits: int | None = None,
    max_commits: int = 0,
    timeout: int = 1800,
    log: logging.Logger | None = None,
    write_json: Callable[[Path, Any], None] | None = None,
) -> int:
    """Per-commit file churn as JSONL, same record shape as the Git exporter.

    One ``hg log -p --git`` stream for the whole history. Merge changesets are
    written without files, as ``git log --numstat`` does. Past ``max_commits``
    only the newest changesets get per-file detail (totals stay complete).
    """
    capped = bool(max_commits and total_commits and total_commits > max_commits)
    head = SEP.join(
        ["{node}", "{author|person}", "{author|email}", "{date|rfc3339date}",
         "{desc|firstline}", "{p2node}"]
    )
    template = "COMMIT" + SEP + head + "\n"
    args = ["log", "-r", "reverse(all())", "--git", "-p", "-T", template]
    if capped:
        args[1:1] = ["-l", str(max_commits)]
    cmd = _hg_cmd(repo, *args)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    buf = ""
    header: list[str] | None = None
    body: list[str] = []

    def flush(fh: Any) -> int:
        if header is None:
            return 0
        sha, name, email, date, subject, p2 = (header + [""] * 6)[:6]
        email = _clean_email(email)
        files = [] if p2 != NULL_NODE else _diff_files(body)
        rec = {
            "sha": sha,
            "author_name": name,
            "author_email": email,
            "author_date": date,
            "subject": subject,
            "is_bot": bool(is_bot(name, email)),
            "files": files,
            "additions": sum(f["additions"] for f in files),
            "deletions": sum(f["deletions"] for f in files),
        }
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return 1

    def feed(fh: Any, line: str) -> int:
        nonlocal header, body
        if line.startswith("COMMIT" + SEP):
            n = flush(fh)
            header = line.split(SEP)[1:]
            body = []
            return n
        body.append(line)
        return 0

    with out_path.open("w", encoding="utf-8") as fh:
        for chunk in _stream(cmd, timeout, log):
            buf += chunk
            *whole, buf = buf.split("\n")
            for line in whole:
                count += feed(fh, line)
        if buf:
            count += feed(fh, buf)
        count += flush(fh)

    if capped:
        if log is not None:
            log.warning(
                "commits.jsonl: per-file detail for the newest %s of %s changesets "
                "(large history, cost cap); totals/authors/dates are complete.",
                max_commits,
                total_commits,
            )
        if write_json is not None:
            write_json(
                out_path.parent / "commits_detail_meta.json",
                {
                    "capped": True,
                    "total_commits": total_commits,
                    "detail_commits_exported": count,
                    "detail_limit": max_commits,
                    "note": (
                        "Per-changeset file churn was limited to the newest "
                        "changesets to keep cost bounded."
                    ),
                },
            )
    return count
