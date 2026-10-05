"""`for-check.csv`: source-side facts recorded while the platform is still reachable.

The extract alone cannot say what it *should* contain: a repo with 294 PRs and a summary row that says 12 looks
valid on its own. This module asks the platform (GitHub / GitLab API and `git ls-remote`) for counts and refs just
before and just after a repo is processed, and records what the tool itself captured, so a checker (Garmr) can
verify the extract against facts it could not recover from git history.

One long-format CSV per run, beside summary.csv:   repo,section,key,value
The same rows are the second sheet ("for-check") of summary.xlsx.

  meta          platform, host, started_at, finished_at, format_version
  counts_before entity counts before the repo was processed        (blank value = unknown, never a guess)
  counts_after  entity counts after it finished
  ref_before    branches, tags, PR/MR head refs on the source -> sha, before / after
  ref_after
  captured      what this tool recorded for the repo (commits, merged_prs, contributors, default_branch, size_kb)
  file          every file this run wrote under api/<repo>/ and git/<repo>/ -> size in bytes
  error         anything that could not be collected (non-fatal; collection never fails the extract)

Per-repo facts are persisted to api/<repo>/for_check.json as soon as the repo is done, so --resume runs keep the
rows of repos that are not re-processed. Metadata only: no source text is stored.
"""

from __future__ import annotations

import base64
import csv
import json
import os
import re
import subprocess
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from count_merged_prs import (
    github_api,
    github_headers,
    gitlab_api,
    http_get_json,
    http_post_json,
)

COLUMNS = ["repo", "section", "key", "value"]
FORMAT_VERSION = 1
FILE_NAME = "for-check.csv"
SHEET_NAME = "for-check"
REF_KEEP = re.compile(r"^refs/(heads/.+|tags/.+|pull/\d+/head|merge-requests/\d+/head)$")
LS_REMOTE_TIMEOUT_SECONDS = 600
COUNT_KEYS_GITHUB = (
    "prs", "issues", "labels", "milestones", "releases", "branches", "tags", "commits", "default_branch",
    "size_kb", "has_wiki", "has_issues", "issue_comments", "review_comments", "commit_comments", "collaborators",
)
COUNT_KEYS_GITLAB = (
    "prs", "issues", "labels", "milestones", "releases", "branches", "tags", "commits", "default_branch",
    "collaborators", "has_wiki",
)

GITHUB_QUERY = """
query($o:String!,$r:String!){repository(owner:$o,name:$r){
  defaultBranchRef{name target{... on Commit{history{totalCount}}}}
  pullRequests{totalCount} issues{totalCount} labels{totalCount} milestones{totalCount} releases{totalCount}
  branches:refs(refPrefix:"refs/heads/"){totalCount} tags:refs(refPrefix:"refs/tags/"){totalCount}
  diskUsage hasWikiEnabled hasIssuesEnabled}}
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _err(exc: BaseException) -> str:
    text = str(exc).splitlines()[0] if str(exc) else ""
    return f"{type(exc).__name__}: {text}"[:300]


def row(repo: str, section: str, key: Any, value: Any = "") -> dict[str, str]:
    return {"repo": repo, "section": section, "key": str(key), "value": "" if value is None else str(value)}


# ---------------------------------------------------------------- HTTP helpers

def _header(headers: dict[str, str], name: str) -> str:
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return ""


def _count(url: str, headers: dict[str, str]) -> int | str:
    """Item count of a list endpoint using one item per page: X-Total (GitLab) or the rel=last page (GitHub)."""
    data, hdr = http_get_json(url + ("&" if "?" in url else "?") + "per_page=1", headers)
    total = _header(hdr, "X-Total")
    if total:
        return int(total)
    match = re.search(r'[?&]page=(\d+)>;\s*rel="last"', _header(hdr, "Link"))
    if match:
        return int(match.group(1))
    return len(data) if isinstance(data, list) else ""


def _try(errors: list[str], label: str, fn: Callable, *args: Any) -> Any:
    try:
        return fn(*args)
    except Exception as exc:  # unknown stays blank: a wrong guess would hide a real gap
        errors.append(f"{label}: {_err(exc)}")
        return ""


def _graphql_url(host: str) -> str:
    return "https://api.github.com/graphql" if host == "github.com" else f"https://{host}/api/graphql"


def _github_graphql(host: str, token: str, owner: str, repo: str) -> dict[str, Any]:
    data = http_post_json(
        _graphql_url(host),
        github_headers(token),
        {"query": GITHUB_QUERY, "variables": {"o": owner, "r": repo}},
    )
    if not isinstance(data, dict) or data.get("errors") or not data.get("data"):
        raise RuntimeError(f"graphql: {json.dumps((data or {}).get('errors', data))[:200]}")
    return data["data"]["repository"]


# ---------------------------------------------------------------- counts

def github_counts(full_name: str, *, token: str, host: str) -> dict[str, Any]:
    errors: list[str] = []
    owner, _, repo = full_name.partition("/")
    counts: dict[str, Any] = {k: "" for k in COUNT_KEYS_GITHUB}
    try:
        r = _github_graphql(host, token, owner, repo)
        ref = r.get("defaultBranchRef")
        counts.update(
            prs=r["pullRequests"]["totalCount"], issues=r["issues"]["totalCount"],
            labels=r["labels"]["totalCount"], milestones=r["milestones"]["totalCount"],
            releases=r["releases"]["totalCount"], branches=r["branches"]["totalCount"],
            tags=r["tags"]["totalCount"], size_kb=r.get("diskUsage", ""),
            commits=ref["target"]["history"]["totalCount"] if ref else 0,
            default_branch=ref["name"] if ref else "",
            has_wiki=str(bool(r.get("hasWikiEnabled"))).lower(),
            has_issues=str(bool(r.get("hasIssuesEnabled"))).lower(),
        )
    except Exception as exc:
        errors.append(f"graphql: {_err(exc)}")
    headers = github_headers(token)
    base = f"{github_api(token, host)}/repos/{full_name}"
    counts["issue_comments"] = _try(errors, "issue_comments", _count, f"{base}/issues/comments", headers)
    counts["review_comments"] = _try(errors, "review_comments", _count, f"{base}/pulls/comments", headers)
    counts["commit_comments"] = _try(errors, "commit_comments", _count, f"{base}/comments", headers)
    counts["collaborators"] = _try(errors, "collaborators", _count, f"{base}/collaborators?affiliation=all", headers)
    counts["_errors"] = errors
    return counts


def gitlab_counts(project: str, *, token: str, host: str, project_id: Any = None) -> dict[str, Any]:
    errors: list[str] = []
    ident = str(project_id) if project_id else urllib.parse.quote(project, safe="")
    base = f"{gitlab_api(host)}/projects/{ident}"
    headers = {"PRIVATE-TOKEN": token, "User-Agent": "for-check"}
    counts: dict[str, Any] = {k: "" for k in COUNT_KEYS_GITLAB}
    try:
        info, _ = http_get_json(f"{base}?statistics=true", headers)
        counts["commits"] = (info.get("statistics") or {}).get("commit_count", "")
        counts["default_branch"] = info.get("default_branch") or ""
        counts["has_wiki"] = str(bool(info.get("wiki_enabled"))).lower()
    except Exception as exc:
        errors.append(f"project: {_err(exc)}")
    for key, path in (
        ("issues", "issues?scope=all"), ("prs", "merge_requests?scope=all&state=all"),
        ("labels", "labels?include_ancestor_groups=false"), ("milestones", "milestones"),
        ("releases", "releases"), ("branches", "repository/branches"), ("tags", "repository/tags"),
        ("collaborators", "members/all"),
    ):
        counts[key] = _try(errors, key, _count, f"{base}/{path}", headers)
    counts["_errors"] = errors
    return counts


# ---------------------------------------------------------------- refs

def ls_remote_refs(url: str, user: str, token: str, run: Callable = subprocess.run) -> dict[str, str]:
    """Branches, tags and PR/MR head refs on the source. The token travels in the environment, never on argv."""
    auth = base64.b64encode(f"{user}:{token}".encode()).decode()
    env = {
        **os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraHeader", "GIT_CONFIG_VALUE_0": f"Authorization: Basic {auth}",
    }
    res = run(["git", "ls-remote", "--refs", url], capture_output=True, text=True, env=env,
              timeout=LS_REMOTE_TIMEOUT_SECONDS)
    if res.returncode != 0:
        msg = (res.stderr or "").strip().splitlines()
        raise RuntimeError("git ls-remote failed: " + (msg[0].replace(token, "***") if msg else f"exit {res.returncode}"))
    refs: dict[str, str] = {}
    for line in res.stdout.splitlines():
        sha, _, ref = line.partition("\t")
        if ref and REF_KEEP.match(ref):
            refs[ref] = sha
    return refs


def _remote_url(platform: str, full_name: str, host: str) -> tuple[str, str]:
    """(git URL without credentials, basic-auth user) for `git ls-remote`."""
    if platform == "github":
        return f"https://{host}/{full_name}.git", "x-access-token"
    if platform == "gitlab":
        h = host.rstrip("/")
        scheme = "http://" if h.startswith("http://") else "https://"
        return f"{scheme}{h.replace('https://', '').replace('http://', '')}/{full_name}.git", "oauth2"
    return f"https://bitbucket.org/{full_name}.git", "x-token-auth"


# ---------------------------------------------------------------- per-repo snapshot

def snapshot(platform: str, full_name: str, *, token: str, host: str, project_id: Any = None) -> dict[str, Any]:
    """Counts + refs for one repo right now. Never raises: failures become `errors`."""
    errors: list[str] = []
    counts: dict[str, Any] = {}
    refs: dict[str, str] = {}
    try:
        if platform == "github":
            counts = github_counts(full_name, token=token, host=host)
        elif platform == "gitlab":
            counts = gitlab_counts(full_name, token=token, host=host, project_id=project_id)
        else:
            errors.append(f"counts: not collected for {platform}")
        errors += counts.pop("_errors", [])
    except Exception as exc:
        errors.append(f"counts: {_err(exc)}")
    try:
        url, user = _remote_url(platform, full_name, host)
        refs = ls_remote_refs(url, user, token)
    except Exception as exc:
        errors.append(f"refs: {_err(exc)}")
    return {"counts": counts, "refs": refs, "errors": errors}


def unavailable(reason: str) -> dict[str, Any]:
    return {"counts": {}, "refs": {}, "errors": [reason]}


def save_repo(api_dir: Path, *, platform: str, host: str, started_at: str, before: dict[str, Any],
              after: dict[str, Any], captured: dict[str, Any]) -> None:
    """Persist one repo's facts. Whatever goes wrong here must never fail the extract."""
    try:
        payload = {"platform": platform, "host": host, "started_at": started_at, "finished_at": now(),
                   "before": before, "after": after, "captured": captured}
        (api_dir / "for_check.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------- run-level CSV

def _repo_files(run_dir: Path, slug: str) -> dict[str, int]:
    files: dict[str, int] = {}
    for sub in ("api", "git"):
        base = run_dir / sub / slug
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if path.is_file() and path.name != "for_check.json":
                files[path.relative_to(run_dir).as_posix()] = path.stat().st_size
    return files


def build_rows(run_dir: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for fc_path in sorted((run_dir / "api").glob("*/for_check.json")):
        slug = fc_path.parent.name
        try:
            info = json.loads(fc_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            rows.append(row(slug, "error", "for_check", f"unreadable {fc_path.name}: {_err(exc)}"))
            continue
        captured = info.get("captured") or {}
        label = captured.get("repo") or slug
        rows.append(row(label, "meta", "format_version", FORMAT_VERSION))
        for key in ("platform", "host", "started_at", "finished_at"):
            rows.append(row(label, "meta", key, info.get(key, "")))
        for tag in ("before", "after"):
            snap = info.get(tag) or {}
            rows += [row(label, f"counts_{tag}", k, v) for k, v in (snap.get("counts") or {}).items()]
            rows += [row(label, f"ref_{tag}", k, v) for k, v in sorted((snap.get("refs") or {}).items())]
            rows += [row(label, "error", tag, e) for e in snap.get("errors", [])]
        rows += [row(label, "captured", k, v) for k, v in captured.items() if k != "repo"]
        rows += [row(label, "file", k, v) for k, v in _repo_files(run_dir, slug).items()]
    return rows


def write_csv(path: Path, rows: list[dict[str, str]]) -> Path:
    partial = path.with_suffix(path.suffix + ".part")
    with partial.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    partial.replace(path)
    return path


def write_run_csv(run_dir: Path) -> Path | None:
    """Rebuild <run_dir>/for-check.csv from the per-repo facts. Never raises."""
    try:
        return write_csv(run_dir / FILE_NAME, build_rows(run_dir))
    except Exception:
        return None


def write_workbook(summary_csv: Path) -> Path | None:
    """summary.xlsx = sheet "summary" (summary.csv) + sheet "for-check" (for-check.csv when present)."""
    try:
        import openpyxl
    except ImportError:
        return None
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "summary"
    with summary_csv.open(newline="", encoding="utf-8") as handle:
        for line in csv.reader(handle):
            sheet.append(line)
    fc_csv = summary_csv.with_name(FILE_NAME)
    if fc_csv.is_file():
        fc_sheet = workbook.create_sheet(SHEET_NAME)
        with fc_csv.open(newline="", encoding="utf-8") as handle:
            for line in csv.reader(handle):
                fc_sheet.append(line)
    xlsx_path = summary_csv.with_suffix(".xlsx")
    workbook.save(xlsx_path)
    return xlsx_path
