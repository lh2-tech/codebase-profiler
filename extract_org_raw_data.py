#!/usr/bin/env python3
"""
Extract org/repo raw inputs (API + full git clone) and write a summary overview.

No LLM / no scoring stages. Raw payloads are JSON/JSONL only; overview is summary.csv.
Clones are deleted before the final zip.

Usage:
  # List orgs/users that installed the GitHub App
  python extract_org_raw_data.py --list-installations

  # Extract via GitHub App (recommended for customer installs)
  python extract_org_raw_data.py --github-app --github-org CustomerOrg --workers 10

  # Or pass installation id explicitly
  python extract_org_raw_data.py --github-app --installation-id 123456 \\
      --github-org CustomerOrg --workers 10

  # Legacy PAT mode
  python extract_org_raw_data.py --github-org Aurelium-Inc-LH2 --tokens-file tokens \\
      --github-token-name data-lh2-github-token --workers 10
  python extract_org_raw_data.py --gitlab-group my-group --tokens-file tokens --workers 8
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import urllib.parse
import webbrowser
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from count_merged_prs import (
    github_api,
    github_graphql,
    github_headers,
    gitlab_api,
    http_get_json,
    http_post_json,
    paginate_github,
    paginate_gitlab,
)
from github_app_auth import (
    DEFAULT_GITHUB_APP_ID,
    DEFAULT_GITHUB_APP_PEM,
    InstallationTokenProvider,
    find_installation_id,
    list_installations,
)

CODING = Path(__file__).resolve().parent
DEFAULT_GITHUB_TOKEN_NAME = "github-data-token"
DEFAULT_GITLAB_TOKEN_NAME = "gitlab_token"
GITHUB_APP_TOKEN_KEY = "__github_app_installation_token__"

BOT_NAME_PATTERNS = [
    re.compile(r, re.I)
    for r in [
        r"\bdependabot\b",
        r"\brenovate\b",
        r"\bsnyk-bot\b",
        r"\bgithub-actions\b",
        r"\bmergify\b",
        r"\bpre-commit-ci\b",
        r"\bwhitesource\b",
        r"\bgreenkeeper\b",
        r"\bimgbot\b",
        r"\b\[bot\]\b",
    ]
]

CLONE_CREDENTIAL_RE = re.compile(r"(https?://)(?:x-access-token|oauth2):[^@/\s]+@", re.I)
GITHUB_MERGE_RE = re.compile(r"^Merge pull request #(\d+) from (\S+)")
GITHUB_SQUASH_RE = re.compile(r"\(#(\d+)\)$")
GITLAB_MR_RE = re.compile(r"See merge request (?:\S+)?!(\d+)")
BITBUCKET_MERGE_RE = re.compile(r"^Merged in (.+) \(pull request #(\d+)\)$")

GITHUB_MERGED_PRS_QUERY = """
query MergedPRs($owner: String!, $name: String!, $cursor: String, $pageSize: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequests(
      states: MERGED,
      first: $pageSize,
      after: $cursor,
      orderBy: {field: UPDATED_AT, direction: DESC}
    ) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number
        title
        bodyText
        url
        createdAt
        mergedAt
        changedFiles
        additions
        deletions
        totalCommentsCount
        author { login __typename }
        labels(first: 20) { nodes { name } }
        commits { totalCount }
        files(first: 60) {
          pageInfo { hasNextPage }
          nodes { path }
        }
        closingIssuesReferences(first: 10) {
          nodes { number title url }
        }
        comments(first: 25) {
          pageInfo { hasNextPage }
          nodes { bodyText createdAt author { login __typename } }
        }
        reviews(first: 25) {
          pageInfo { hasNextPage }
          nodes {
            state
            bodyText
            createdAt
            author { login __typename }
          }
        }
        reviewThreads(first: 30) {
          pageInfo { hasNextPage }
          nodes {
            isResolved
            comments(first: 20) {
              pageInfo { hasNextPage }
              nodes { bodyText path createdAt author { login __typename } }
            }
          }
        }
      }
    }
  }
}
"""

SUMMARY_FIELDS = [
    "org",
    "repo",
    "merged_prs",
    "languages_breakdown",
    "repo_created_at",
    "last_commit_date",
    "loc",
    "primary_language",
    "size_kb",
    "default_branch",
    "total_commits",
    "first_commit",
    "span_days",
    "recency_days",
    "contributor_count",
    "human_authors",
    "bot_authors",
    "bot_commit_ratio",
    "has_tests",
    "codebase_description",
    "industry_domain",
    "vibe_code_signals",
    "repo_type",
    "llm_analysis_error",
    "error",
]

TEST_DIR_HINTS = ("test", "tests", "spec", "specs", "__tests__")
SKIP_WALK_DIRS = {".git", "node_modules", "vendor", "dist", "build", ".venv", "venv"}
LLM_SEMAPHORE = threading.BoundedSemaphore(3)
LLM_SOURCE_EXTENSIONS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".go", ".rs", ".rb", ".php",
    ".cs", ".swift", ".kt", ".scala", ".vue", ".svelte",
}


@dataclass(frozen=True)
class LLMConfig:
    api_key: str
    model: str


@dataclass
class RepoTarget:
    platform: str  # github | gitlab | local
    org: str
    full_name: str
    meta: dict[str, Any]
    local_path: Path | None = None


def parse_tokens_file(path: Path) -> dict[str, str]:
    tokens: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        tokens[key.strip()] = value.strip()
    return tokens


def safe_name(name: str) -> str:
    return re.sub(r"[^\w.\-]+", "_", name)


def setup_logger(log_path: Path) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("raw_extract")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def is_bot_author(name: str, email: str = "") -> bool:
    return any(p.search(f"{name} {email}") for p in BOT_NAME_PATTERNS)


def is_bot_login(login: str | None, typename: str | None = None) -> bool:
    if typename == "Bot":
        return True
    if not login:
        return False
    low = login.lower()
    return low.endswith("[bot]") or "bot" in low or is_bot_author(login)


def languages_breakdown_str(langs: dict[str, Any]) -> str:
    if not langs:
        return ""
    values = {k: float(v) for k, v in langs.items() if v is not None}
    total = sum(values.values())
    if total <= 0:
        return ""
    parts = []
    for name, val in sorted(values.items(), key=lambda kv: kv[1], reverse=True):
        pct = 100.0 * val / total
        parts.append(f"{name}:{pct:.1f}%")
    return "; ".join(parts)


# ── discovery ───────────────────────────────────────────────────────────────


def list_github_repo_objects(token: str, org: str, host: str) -> list[dict[str, Any]]:
    api = github_api(token, host)
    repos: list[dict[str, Any]] = []
    for kind in (f"orgs/{org}/repos", f"users/{org}/repos"):
        batch = paginate_github(f"{api}/{kind}?per_page=100&type=all", token)
        if batch:
            repos = batch
            break
    if not repos:
        raise RuntimeError(f"No GitHub repos found for {org!r}")
    return repos


def fetch_github_repo(token: str, full_name: str, host: str) -> dict[str, Any]:
    api = github_api(token, host)
    data, _ = http_get_json(f"{api}/repos/{full_name}", github_headers(token))
    if not isinstance(data, dict):
        raise RuntimeError(f"GitHub repo not found: {full_name}")
    return data


def list_gitlab_project_objects(token: str, group: str, host: str) -> list[dict[str, Any]]:
    api = gitlab_api(host)
    encoded = urllib.parse.quote(group, safe="")
    projects = paginate_gitlab(
        api,
        f"/groups/{encoded}/projects",
        token,
        {"include_subgroups": "true", "archived": "true"},
    )
    if not projects:
        raise RuntimeError(f"No GitLab projects found for group {group!r}")
    return projects


def fetch_gitlab_project(token: str, path: str, host: str) -> dict[str, Any]:
    api = gitlab_api(host)
    encoded = urllib.parse.quote(path, safe="")
    data, _ = http_get_json(
        f"{api}/projects/{encoded}",
        {"PRIVATE-TOKEN": token, "User-Agent": "extract-org-raw-data"},
    )
    if not isinstance(data, dict):
        raise RuntimeError(f"GitLab project not found: {path}")
    return data


# ── API raw extracts ────────────────────────────────────────────────────────


def fetch_github_languages(token: str, full_name: str, host: str) -> dict[str, int]:
    api = github_api(token, host)
    data, _ = http_get_json(f"{api}/repos/{full_name}/languages", github_headers(token))
    return data if isinstance(data, dict) else {}


def fetch_github_contributors(token: str, full_name: str, host: str) -> list[dict[str, Any]]:
    api = github_api(token, host)
    return paginate_github(
        f"{api}/repos/{full_name}/contributors?per_page=100&anon=1",
        token,
    )


def fetch_github_merged_prs(token: str, full_name: str, host: str) -> list[dict[str, Any]]:
    owner, name = full_name.split("/", 1)
    nodes: list[dict[str, Any]] = []
    cursor = None
    while True:
        payload = {
            "query": GITHUB_MERGED_PRS_QUERY,
            "variables": {
                "owner": owner,
                "name": name,
                "cursor": cursor,
                "pageSize": 25,
            },
        }
        data = http_post_json(github_graphql(token, host), github_headers(token), payload)
        if data.get("errors"):
            raise RuntimeError(str(data["errors"])[:500])
        repo = (data.get("data") or {}).get("repository")
        if not repo:
            raise RuntimeError(f"Repository not found: {full_name}")
        conn = repo["pullRequests"]
        for node in conn.get("nodes") or []:
            author = node.get("author") or {}
            node["author_is_bot"] = is_bot_login(author.get("login"), author.get("__typename"))
            nodes.append(node)
        page = conn.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            break
        cursor = page.get("endCursor")
    return nodes


def fetch_gitlab_languages(token: str, project_id: int | str, host: str) -> dict[str, float]:
    api = gitlab_api(host)
    data, _ = http_get_json(
        f"{api}/projects/{project_id}/languages",
        {"PRIVATE-TOKEN": token, "User-Agent": "extract-org-raw-data"},
    )
    return data if isinstance(data, dict) else {}


def fetch_gitlab_members(token: str, project_id: int | str, host: str) -> list[dict[str, Any]]:
    api = gitlab_api(host)
    return paginate_gitlab(
        api, f"/projects/{project_id}/members/all", token, {"per_page": "100"}
    )


def fetch_gitlab_merged_mrs(
    token: str, project_id: int | str, host: str
) -> list[dict[str, Any]]:
    api = gitlab_api(host)
    mrs = paginate_gitlab(
        api,
        f"/projects/{project_id}/merge_requests",
        token,
        {"state": "merged", "per_page": "100"},
    )
    headers = {"PRIVATE-TOKEN": token, "User-Agent": "extract-org-raw-data"}
    enriched: list[dict[str, Any]] = []
    for mr in mrs:
        iid = mr.get("iid")
        detail = mr
        try:
            d, _ = http_get_json(
                f"{api}/projects/{project_id}/merge_requests/{iid}",
                headers,
            )
            if isinstance(d, dict):
                detail = d
        except Exception:
            pass
        author = (detail.get("author") or {}).get("username") or ""
        detail["author_is_bot"] = is_bot_login(author)
        try:
            detail["notes"] = paginate_gitlab(
                api,
                f"/projects/{project_id}/merge_requests/{iid}/notes",
                token,
                {"per_page": "100"},
            )
        except Exception:
            detail["notes"] = []
        try:
            changes, _ = http_get_json(
                f"{api}/projects/{project_id}/merge_requests/{iid}/changes",
                headers,
            )
            detail["changes"] = changes.get("changes") if isinstance(changes, dict) else []
        except Exception:
            detail["changes"] = []
        enriched.append(detail)
    return enriched


# ── git / scc ───────────────────────────────────────────────────────────────


def run_git(repo: Path, *args: str, timeout: int = 300) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        return ""
    return proc.stdout


def clone_repo(url: str, dest: Path, timeout: int = 900) -> None:
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "GIT_LFS_SKIP_SMUDGE": "1"}
    proc = subprocess.run(
        ["git", "clone", "--recurse-submodules=0", url, str(dest)],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    if proc.returncode != 0:
        detail = proc.stderr or proc.stdout or "clone failed"
        detail = CLONE_CREDENTIAL_RE.sub(r"\1***:***@", detail)
        raise RuntimeError(detail[-800:])


def github_clone_url(full_name: str, token: str, host: str) -> str:
    if host == "github.com":
        return f"https://x-access-token:{token}@github.com/{full_name}.git"
    return f"https://x-access-token:{token}@{host}/{full_name}.git"


def gitlab_clone_url(full_name: str, token: str, host: str) -> str:
    host = host.rstrip("/")
    if not host.startswith("http"):
        host = f"https://{host}"
    bare = host.replace("https://", "").replace("http://", "")
    return f"https://oauth2:{token}@{bare}/{full_name}.git"


def aggregate_git_stats(repo: Path) -> dict[str, Any]:
    total_s = run_git(repo, "rev-list", "--count", "HEAD").strip()
    total_commits = int(total_s) if total_s.isdigit() else 0

    root_sha = run_git(repo, "rev-list", "--max-parents=0", "HEAD").strip().splitlines()
    first = ""
    if root_sha:
        first = run_git(repo, "log", "-1", "--pretty=format:%aI", root_sha[0]).strip()
    last = run_git(repo, "log", "-1", "--pretty=format:%aI").strip()

    authors: list[dict[str, Any]] = []
    for line in run_git(repo, "shortlog", "-sne", "HEAD").splitlines():
        line = line.strip()
        m = re.match(r"^\s*(\d+)\s+(.*?)\s+<(.+)>\s*$", line)
        if not m:
            continue
        count, name, email = m.groups()
        authors.append(
            {
                "name": name,
                "email": email,
                "commits": int(count),
                "is_bot": is_bot_author(name, email),
            }
        )
    human = [a for a in authors if not a["is_bot"]]
    bots = [a for a in authors if a["is_bot"]]
    bot_commit_count = sum(a["commits"] for a in bots)
    bot_ratio = bot_commit_count / total_commits if total_commits else 0.0

    recency_days = None
    span_days = None
    if last:
        try:
            last_dt = datetime.fromisoformat(last)
            recency_days = (
                datetime.now(timezone.utc) - last_dt.astimezone(timezone.utc)
            ).days
        except ValueError:
            pass
    if first and last:
        try:
            span_days = (
                datetime.fromisoformat(last) - datetime.fromisoformat(first)
            ).days
        except ValueError:
            pass

    return {
        "total_commits": total_commits,
        "first_commit": first or None,
        "last_commit": last or None,
        "span_days": span_days,
        "recency_days": recency_days,
        "human_authors": len(human),
        "bot_authors": len(bots),
        "bot_commit_count": bot_commit_count,
        "bot_commit_ratio": round(bot_ratio, 4),
        "authors": authors[:50],
    }


def export_commits_jsonl(repo: Path, out_path: Path) -> int:
    text = run_git(
        repo,
        "log",
        "--pretty=format:COMMIT\t%H\t%aN\t%aE\t%aI\t%s",
        "--numstat",
        timeout=600,
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    current: dict[str, Any] | None = None
    with out_path.open("w", encoding="utf-8") as fh:
        for line in text.splitlines():
            if line.startswith("COMMIT\t"):
                if current is not None:
                    fh.write(json.dumps(current, ensure_ascii=False) + "\n")
                    count += 1
                _, sha, name, email, date, subject = line.split("\t", 5)
                current = {
                    "sha": sha,
                    "author_name": name,
                    "author_email": email,
                    "author_date": date,
                    "subject": subject,
                    "is_bot": is_bot_author(name, email),
                    "files": [],
                    "additions": 0,
                    "deletions": 0,
                }
                continue
            if not line.strip() or current is None:
                continue
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            add_s, del_s, path = parts
            add_n = int(add_s) if add_s.isdigit() else 0
            del_n = int(del_s) if del_s.isdigit() else 0
            current["files"].append(
                {"path": path, "additions": add_n, "deletions": del_n}
            )
            current["additions"] += add_n
            current["deletions"] += del_n
        if current is not None:
            fh.write(json.dumps(current, ensure_ascii=False) + "\n")
            count += 1
    return count


def run_scc(repo: Path) -> dict[str, Any]:
    if not shutil.which("scc"):
        raise RuntimeError("scc not found on PATH")
    proc = subprocess.run(
        ["scc", "--format", "json", str(repo)],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "scc failed")[-500:])
    data = json.loads(proc.stdout or "[]")
    if not isinstance(data, list):
        return {"loc": 0, "by_language": {}, "raw": data}
    by_lang: dict[str, int] = {}
    loc = 0
    for row in data:
        name = row.get("Name") or row.get("name") or "Unknown"
        code = int(row.get("Code") or row.get("code") or 0)
        by_lang[name] = by_lang.get(name, 0) + code
        loc += code
    return {"loc": loc, "by_language": by_lang, "raw": data}


def scc_languages_breakdown(scc: dict[str, Any]) -> tuple[str, str]:
    """Return (primary_language, percentage breakdown) from SCC code LOC."""
    by_language = {
        str(name): int(code)
        for name, code in (scc.get("by_language") or {}).items()
        if int(code) > 0
    }
    total = sum(by_language.values())
    if not total:
        return "", ""
    ordered = sorted(by_language.items(), key=lambda item: (-item[1], item[0]))
    return (
        ordered[0][0],
        "; ".join(
            f"{name}:{100 * code / total:.1f}%" for name, code in ordered
        ),
    )


def working_tree_size_kb(repo: Path) -> int:
    """Size of checked-out files only; excludes git metadata and common build output."""
    total_bytes = 0
    for root, dirs, files in os.walk(repo):
        dirs[:] = [directory for directory in dirs if directory not in SKIP_WALK_DIRS]
        for filename in files:
            try:
                total_bytes += (Path(root) / filename).stat().st_size
            except OSError:
                continue
    return round(total_bytes / 1024)


def detect_merged_prs_from_git(repo: Path) -> list[dict[str, str]]:
    """Recover merge/squash PR/MR markers from Git history without API access."""
    text = run_git(
        repo,
        "log",
        "--pretty=format:%H%x1f%aI%x1f%an%x1f%ae%x1f%s%x1f%b%x1e",
        timeout=600,
    )
    records: dict[str, dict[str, str]] = {}
    for raw_record in text.split("\x1e"):
        fields = raw_record.strip("\n").split("\x1f")
        if len(fields) < 5:
            continue
        sha, date, author_name, author_email, subject = fields[:5]
        body = fields[5] if len(fields) > 5 else ""
        number = method = source_branch = ""
        title = subject
        match = GITHUB_MERGE_RE.match(subject)
        if match:
            number, source_branch = match.groups()
            method = "merge-commit"
            title = body.strip().splitlines()[0] if body.strip() else subject
        elif match := BITBUCKET_MERGE_RE.match(subject):
            source_branch, number = match.groups()
            method = "merge-commit"
        elif match := GITLAB_MR_RE.search(body):
            number = match.group(1)
            method = "merge-commit"
        elif match := GITHUB_SQUASH_RE.search(subject):
            number = match.group(1)
            method = "squash"
            title = GITHUB_SQUASH_RE.sub("", subject).strip()
        if not number:
            continue
        if number in records and records[number]["method"] == "merge-commit":
            continue
        records[number] = {
            "pr_number": number,
            "method": method,
            "source_branch": source_branch,
            "title": title,
            "merged_at": date,
            "merged_by_name": author_name,
            "merged_by_email": author_email,
            "merge_commit": sha,
        }
    return sorted(records.values(), key=lambda record: record["merged_at"])


def detect_tests(repo: Path) -> bool:
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in SKIP_WALK_DIRS]
        base = Path(root).name.lower()
        if any(hint in base for hint in TEST_DIR_HINTS):
            return True
        for f in files:
            fl = f.lower()
            if (
                fl.startswith("test_")
                or fl.endswith("_test.py")
                or fl.endswith(".spec.ts")
                or fl.endswith(".spec.js")
                or fl.endswith("_test.go")
                or fl.endswith("test.java")
            ):
                return True
    return False


def _read_text_sample(path: Path, limit: int) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return ""


def build_llm_evidence(repo: Path, row: dict[str, Any]) -> dict[str, Any]:
    """Create a bounded, transient repository sample for the opt-in LLM call."""
    paths: list[str] = []
    source_samples: list[dict[str, str]] = []
    readme = ""

    for root, dirs, files in os.walk(repo):
        dirs[:] = [directory for directory in dirs if directory not in SKIP_WALK_DIRS]
        for filename in sorted(files):
            path = Path(root) / filename
            relative = path.relative_to(repo).as_posix()
            if len(paths) < 250:
                paths.append(relative)
            if filename.lower().startswith("readme") and not readme:
                readme = _read_text_sample(path, 4_000)
            if (
                len(source_samples) < 4
                and path.suffix.lower() in LLM_SOURCE_EXTENSIONS
                and "test" not in relative.lower()
                and path.stat().st_size <= 250_000
            ):
                content = _read_text_sample(path, 1_500)
                if content:
                    source_samples.append({"path": relative, "content": content})

    return {
        "repository": {
            "name": row.get("repo"),
            "org": row.get("org"),
            "primary_language": row.get("primary_language"),
            "languages_breakdown": row.get("languages_breakdown"),
            "loc": row.get("loc"),
            "total_commits": row.get("total_commits"),
            "has_tests": row.get("has_tests"),
        },
        "readme_excerpt": readme,
        "file_paths": paths,
        "source_excerpts": source_samples,
    }


def run_llm_analysis(
    repo: Path,
    row: dict[str, Any],
    config: LLMConfig,
) -> dict[str, str]:
    """Return a bounded JSON analysis without persisting raw code samples."""
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("openai package is required for --llm mode") from exc

    evidence = build_llm_evidence(repo, row)
    system_prompt = """You classify a software repository from limited evidence.
Return JSON only, with exactly these keys:
- codebase_description: concise one or two sentence description.
- industry_domain: concise industry/domain, or "unknown".
- vibe_code_signals: JSON array of short, evidence-based signals only. Do not claim
  AI generation as fact; use phrases such as "possible generated scaffolding".
- repo_type: exactly one of backend, frontend, fullstack, mobile, data_ml,
  library_sdk, infra_devops, other.
Do not invent facts. Do not reproduce source code, secrets, or long text."""
    with LLM_SEMAPHORE:
        client = OpenAI(api_key=config.api_key)
        completion = client.chat.completions.create(
            model=config.model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": json.dumps(evidence, ensure_ascii=False),
                },
            ],
        )
    content = completion.choices[0].message.content or "{}"
    data = json.loads(content)
    if not isinstance(data, dict):
        raise RuntimeError("LLM response was not a JSON object")
    signals = data.get("vibe_code_signals", [])
    if not isinstance(signals, list):
        signals = [str(signals)]
    allowed_types = {
        "backend", "frontend", "fullstack", "mobile", "data_ml",
        "library_sdk", "infra_devops", "other",
    }
    repo_type = str(data.get("repo_type", "other")).strip().lower()
    return {
        "codebase_description": str(data.get("codebase_description", "")).strip(),
        "industry_domain": str(data.get("industry_domain", "unknown")).strip() or "unknown",
        "vibe_code_signals": json.dumps(
            [str(signal).strip() for signal in signals if str(signal).strip()],
            ensure_ascii=False,
        ),
        "repo_type": repo_type if repo_type in allowed_types else "other",
    }


def find_local_repositories(root: Path) -> list[RepoTarget]:
    """Discover working trees beneath ``root`` without reading any remote service."""
    if not root.is_dir():
        raise SystemExit(f"Local repositories directory not found: {root}")

    repos: list[RepoTarget] = []
    for current, dirs, _ in os.walk(root):
        current_path = Path(current)
        if (current_path / ".git").exists():
            relative_path = current_path.relative_to(root)
            repos.append(
                RepoTarget(
                    platform="local",
                    org=root.name,
                    full_name=root.name if relative_path == Path(".") else str(relative_path),
                    meta={},
                    local_path=current_path,
                )
            )
            dirs[:] = []
            continue
        dirs[:] = [
            directory
            for directory in dirs
            if directory not in SKIP_WALK_DIRS and not directory.startswith(".")
        ]
    if not repos:
        raise SystemExit(f"No Git repositories found beneath: {root}")
    return sorted(repos, key=lambda repo: repo.full_name.lower())


def discover_local_repositories(root: Path) -> list[RepoTarget]:
    """Like ``find_local_repositories`` but returns an empty list when nothing is found."""
    if not root.is_dir():
        return []
    repos: list[RepoTarget] = []
    for current, dirs, _ in os.walk(root):
        current_path = Path(current)
        if (current_path / ".git").exists():
            relative_path = current_path.relative_to(root)
            repos.append(
                RepoTarget(
                    platform="local",
                    org=root.name,
                    full_name=root.name if relative_path == Path(".") else str(relative_path),
                    meta={},
                    local_path=current_path,
                )
            )
            dirs[:] = []
            continue
        dirs[:] = [
            directory
            for directory in dirs
            if directory not in SKIP_WALK_DIRS and not directory.startswith(".")
        ]
    return sorted(repos, key=lambda repo: repo.full_name.lower())


def list_github_orgs_for_token(token: str, host: str = "github.com") -> list[dict[str, str]]:
    api = github_api(token, host)
    orgs: list[dict[str, str]] = []
    user_data, _ = http_get_json(f"{api}/user", github_headers(token))
    if isinstance(user_data, dict) and user_data.get("login"):
        login = str(user_data["login"])
        orgs.append(
            {
                "id": login,
                "name": f"{login} (personal account)",
                "type": "user",
            }
        )
    for org in paginate_github(f"{api}/user/orgs?per_page=100", token):
        login = str(org.get("login") or "")
        if login:
            orgs.append({"id": login, "name": login, "type": "org"})
    return orgs


def list_github_repos_for_org(token: str, org: str, host: str = "github.com") -> list[dict[str, str]]:
    return [
        {
            "id": str(repo["full_name"]),
            "name": str(repo["full_name"]),
            "archived": bool(repo.get("archived")),
        }
        for repo in list_github_repo_objects(token, org, host)
    ]


def list_gitlab_groups_for_token(token: str, host: str = "gitlab.com") -> list[dict[str, str]]:
    api = gitlab_api(host)
    groups = paginate_gitlab(
        api,
        "/groups",
        token,
        {"membership": "true", "min_access_level": "10"},
    )
    results: list[dict[str, str]] = []
    for group in groups:
        group_id = str(group.get("full_path") or group.get("path") or "")
        if group_id:
            results.append({"id": group_id, "name": group_id, "type": "group"})
    return sorted(results, key=lambda item: item["name"].lower())


def list_gitlab_projects_for_group(
    token: str, group: str, host: str = "gitlab.com"
) -> list[dict[str, str]]:
    return [
        {
            "id": str(project["path_with_namespace"]),
            "name": str(project["path_with_namespace"]),
            "archived": bool(project.get("archived")),
        }
        for project in list_gitlab_project_objects(token, group, host)
    ]


def filter_local_targets(
    targets: list[RepoTarget], selectors: list[str]
) -> list[RepoTarget]:
    """Keep only local repos matching any selector (full path or folder name)."""
    wanted = [item.strip().lower() for item in selectors if item.strip()]
    if not wanted:
        return targets

    def matches(target: RepoTarget) -> bool:
        full_name = target.full_name.lower()
        repo_name = full_name.split("/")[-1]
        for selector in wanted:
            if full_name == selector:
                return True
            if repo_name == selector:
                return True
            if full_name.endswith(f"/{selector}"):
                return True
        return False

    filtered = [target for target in targets if matches(target)]
    if not filtered:
        raise SystemExit(
            "No local repositories matched the requested selection. "
            f"Available: {', '.join(target.full_name for target in targets)}"
        )
    return filtered


# ── per-repo orchestration ──────────────────────────────────────────────────


def empty_summary_row(org: str, repo: str) -> dict[str, Any]:
    row: dict[str, Any] = {k: "" for k in SUMMARY_FIELDS}
    row.update(
        {
            "org": org,
            "repo": repo,
            "merged_prs": 0,
            "loc": 0,
            "has_tests": False,
            "error": "",
        }
    )
    return row


def process_repo(
    target: RepoTarget,
    *,
    tokens: dict[str, str],
    github_token_name: str,
    gitlab_token_name: str,
    github_token_fn: Callable[[], str] | None,
    llm_config: LLMConfig | None,
    run_dir: Path,
    clones_dir: Path,
    github_host: str,
    gitlab_host: str,
    log: logging.Logger,
) -> dict[str, Any]:
    org = target.org
    repo = target.full_name.split("/")[-1]
    slug = safe_name(target.full_name.replace("/", "__"))
    api_dir = run_dir / "api" / slug
    git_dir = run_dir / "git" / slug
    api_dir.mkdir(parents=True, exist_ok=True)
    git_dir.mkdir(parents=True, exist_ok=True)

    row = empty_summary_row(org, repo)
    meta = target.meta
    if target.platform != "local":
        write_json(api_dir / "repo.json", meta)
    clone_path = target.local_path or clones_dir / slug

    try:
        if target.platform == "github":
            token = github_token_fn() if github_token_fn else tokens[github_token_name]
            row["repo_created_at"] = meta.get("created_at") or ""
            row["primary_language"] = meta.get("language") or ""
            row["size_kb"] = meta.get("size") or 0
            row["default_branch"] = meta.get("default_branch") or ""

            langs = fetch_github_languages(token, target.full_name, github_host)
            write_json(api_dir / "languages.json", langs)
            row["languages_breakdown"] = languages_breakdown_str(langs)

            contributors = fetch_github_contributors(token, target.full_name, github_host)
            write_json(api_dir / "contributors.json", contributors)
            row["contributor_count"] = len(contributors)

            prs = fetch_github_merged_prs(token, target.full_name, github_host)
            write_json(api_dir / "merged_prs.json", prs)
            row["merged_prs"] = len(prs)

            clone_url = github_clone_url(target.full_name, token, github_host)
        elif target.platform == "gitlab":
            token = tokens[gitlab_token_name]
            project_id = meta.get("id")
            row["repo_created_at"] = meta.get("created_at") or ""
            row["primary_language"] = ""
            stats = meta.get("statistics") or {}
            if stats.get("repository_size") is not None:
                row["size_kb"] = int(stats["repository_size"] / 1024)
            row["default_branch"] = meta.get("default_branch") or ""

            langs = fetch_gitlab_languages(token, project_id, gitlab_host)
            write_json(api_dir / "languages.json", langs)
            row["languages_breakdown"] = languages_breakdown_str(langs)

            members = fetch_gitlab_members(token, project_id, gitlab_host)
            write_json(api_dir / "contributors.json", members)
            row["contributor_count"] = len(members)

            mrs = fetch_gitlab_merged_mrs(token, project_id, gitlab_host)
            write_json(api_dir / "merged_prs.json", mrs)
            row["merged_prs"] = len(mrs)

            clone_url = gitlab_clone_url(target.full_name, token, gitlab_host)
        else:
            if target.local_path is None:
                raise RuntimeError("Local repository path is missing")
            row["repo_created_at"] = ""
            row["primary_language"] = ""
            row["size_kb"] = ""
            row["default_branch"] = (
                run_git(clone_path, "symbolic-ref", "--short", "HEAD").strip()
                or "not_collected_offline"
            )
            row["merged_prs"] = 0
            row["contributor_count"] = 0

        if target.platform != "local":
            clone_repo(clone_url, clone_path)

        git_stats = aggregate_git_stats(clone_path)
        write_json(git_dir / "git_stats.json", git_stats)
        row["total_commits"] = git_stats["total_commits"]
        row["first_commit"] = git_stats["first_commit"] or ""
        row["last_commit_date"] = git_stats["last_commit"] or ""
        row["span_days"] = (
            git_stats["span_days"] if git_stats["span_days"] is not None else ""
        )
        row["recency_days"] = (
            git_stats["recency_days"] if git_stats["recency_days"] is not None else ""
        )
        row["human_authors"] = git_stats["human_authors"]
        row["bot_authors"] = git_stats["bot_authors"]
        row["bot_commit_ratio"] = git_stats["bot_commit_ratio"]

        export_commits_jsonl(clone_path, git_dir / "commits.jsonl")

        scc = run_scc(clone_path)
        write_json(git_dir / "scc.json", scc)
        row["loc"] = scc["loc"]

        if target.platform == "local":
            primary_language, language_breakdown = scc_languages_breakdown(scc)
            detected_prs = detect_merged_prs_from_git(clone_path)
            write_json(git_dir / "merged_prs_detected_from_git.json", detected_prs)
            row["repo_created_at"] = git_stats["first_commit"] or ""
            row["primary_language"] = primary_language
            row["languages_breakdown"] = language_breakdown
            row["size_kb"] = working_tree_size_kb(clone_path)
            row["merged_prs"] = len(detected_prs)
            row["contributor_count"] = (
                git_stats["human_authors"] + git_stats["bot_authors"]
            )

        row["has_tests"] = detect_tests(clone_path)
        if llm_config is not None:
            try:
                row.update(run_llm_analysis(clone_path, row, llm_config))
            except Exception as exc:
                row["llm_analysis_error"] = str(exc)[:500]

        if target.platform != "local":
            shutil.rmtree(clone_path, ignore_errors=True)
        log.info(
            "OK %s: merged_prs=%s loc=%s span_days=%s",
            target.full_name,
            row["merged_prs"],
            row["loc"],
            row["span_days"],
        )
    except Exception as exc:
        row["error"] = str(exc)[:500]
        log.error("FAIL %s: %s", target.full_name, row["error"])
        if target.platform != "local":
            shutil.rmtree(clone_path, ignore_errors=True)

    return row


def zip_run_dir(run_dir: Path) -> Path:
    zip_path = run_dir.with_suffix(".zip")
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in run_dir.rglob("*"):
            if path.is_file():
                zf.write(path, path.relative_to(run_dir.parent).as_posix())
    return zip_path


def resolve_github_token(
    tokens: dict[str, str],
    github_token_name: str,
    github_token_fn: Callable[[], str] | None,
) -> str:
    if github_token_fn is not None:
        return github_token_fn()
    if github_token_name not in tokens:
        raise SystemExit(f"Missing {github_token_name!r} in tokens file")
    return tokens[github_token_name]


def build_targets(
    args: argparse.Namespace,
    tokens: dict[str, str],
    github_token_name: str,
    gitlab_token_name: str,
    github_token_fn: Callable[[], str] | None = None,
) -> list[RepoTarget]:
    targets: list[RepoTarget] = []

    for org in args.github_org or []:
        gh_token = resolve_github_token(tokens, github_token_name, github_token_fn)
        for meta in list_github_repo_objects(gh_token, org, args.github_host):
            targets.append(
                RepoTarget(
                    platform="github",
                    org=org,
                    full_name=meta["full_name"],
                    meta=meta,
                )
            )

    for full_name in args.github_repo or []:
        gh_token = resolve_github_token(tokens, github_token_name, github_token_fn)
        meta = fetch_github_repo(gh_token, full_name.strip("/"), args.github_host)
        targets.append(
            RepoTarget(
                platform="github",
                org=meta["full_name"].split("/", 1)[0],
                full_name=meta["full_name"],
                meta=meta,
            )
        )

    for group in args.gitlab_group or []:
        if gitlab_token_name not in tokens:
            raise SystemExit(f"Missing {gitlab_token_name!r} in tokens file")
        for meta in list_gitlab_project_objects(
            tokens[gitlab_token_name], group, args.gitlab_host
        ):
            targets.append(
                RepoTarget(
                    platform="gitlab",
                    org=group,
                    full_name=meta["path_with_namespace"],
                    meta=meta,
                )
            )

    for path in args.gitlab_project or []:
        if gitlab_token_name not in tokens:
            raise SystemExit(f"Missing {gitlab_token_name!r} in tokens file")
        meta = fetch_gitlab_project(
            tokens[gitlab_token_name], path.strip("/"), args.gitlab_host
        )
        targets.append(
            RepoTarget(
                platform="gitlab",
                org=meta["path_with_namespace"].split("/", 1)[0],
                full_name=meta["path_with_namespace"],
                meta=meta,
            )
        )

    if not targets:
        raise SystemExit(
            "Provide --github-org / --github-repo / --gitlab-group / --gitlab-project"
        )
    return targets


def resolve_app_settings(
    args: argparse.Namespace,
    tokens: dict[str, str],
) -> tuple[str, Path]:
    app_id = (
        args.github_app_id
        or tokens.get("github_app_id")
        or os.environ.get("GITHUB_APP_ID")
        or DEFAULT_GITHUB_APP_ID
    )
    pem_raw = (
        str(args.github_app_pem)
        if args.github_app_pem
        else tokens.get("github_app_pem")
        or os.environ.get("GITHUB_APP_PEM")
        or str(DEFAULT_GITHUB_APP_PEM)
    )
    pem_path = Path(pem_raw)
    if not pem_path.is_absolute():
        pem_path = (CODING / pem_path).resolve()
    return str(app_id), pem_path


def build_github_app_token_fn(
    args: argparse.Namespace,
    tokens: dict[str, str],
) -> tuple[Callable[[], str], int, str]:
    """Return (token_fn, installation_id, account_login_hint)."""
    app_id, pem_path = resolve_app_settings(args, tokens)
    installation_id = args.installation_id
    account_hint = ""

    if installation_id is None:
        # Prefer explicit org from CLI to resolve installation.
        candidates = list(args.github_org or [])
        for repo in args.github_repo or []:
            candidates.append(repo.strip("/").split("/", 1)[0])
        if not candidates:
            raise SystemExit(
                "--github-app requires --installation-id or --github-org/--github-repo "
                "to resolve the installation"
            )
        account_hint = candidates[0]
        installation_id = find_installation_id(
            app_id, pem_path, account_hint, host=args.github_host
        )
    else:
        # Best-effort label for logs
        for inst in list_installations(app_id, pem_path, host=args.github_host):
            if int(inst["installation_id"]) == int(installation_id):
                account_hint = inst.get("account_login") or ""
                break

    provider = InstallationTokenProvider(
        app_id,
        pem_path,
        installation_id,
        host=args.github_host,
    )
    return provider.get, int(installation_id), account_hint


def cmd_list_installations(args: argparse.Namespace, tokens: dict[str, str]) -> int:
    app_id, pem_path = resolve_app_settings(args, tokens)
    installs = list_installations(app_id, pem_path, host=args.github_host)
    rows = [
        {
            "installation_id": i["installation_id"],
            "account_login": i["account_login"],
            "account_type": i["account_type"],
            "repository_selection": i["repository_selection"],
            "suspended_at": i["suspended_at"],
            "html_url": i["html_url"],
        }
        for i in installs
    ]
    print(json.dumps({"app_id": app_id, "count": len(rows), "installations": rows}, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Extract org raw API + git data; write summary.csv + zip"
    )
    parser.add_argument("--tokens-file", type=Path, default=CODING / "tokens")
    parser.add_argument(
        "--output-dir", type=Path, default=CODING / "outputs" / "raw-extracts"
    )
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--github-host", default=os.environ.get("GITHUB_HOST", "github.com"))
    parser.add_argument("--gitlab-host", default=os.environ.get("GITLAB_HOST", "gitlab.com"))
    parser.add_argument(
        "--github-token-name",
        default=DEFAULT_GITHUB_TOKEN_NAME,
        help=f"Key in tokens file for GitHub PAT mode (default: {DEFAULT_GITHUB_TOKEN_NAME})",
    )
    parser.add_argument(
        "--gitlab-token-name",
        default=DEFAULT_GITLAB_TOKEN_NAME,
        help=f"Key in tokens file for GitLab (default: {DEFAULT_GITLAB_TOKEN_NAME})",
    )
    parser.add_argument(
        "--github-app",
        action="store_true",
        help="Authenticate via GitHub App installation token instead of a PAT",
    )
    parser.add_argument(
        "--github-app-id",
        default=None,
        help=f"GitHub App ID (default: {DEFAULT_GITHUB_APP_ID})",
    )
    parser.add_argument(
        "--github-app-pem",
        type=Path,
        default=None,
        help=f"Path to GitHub App private key PEM (default: {DEFAULT_GITHUB_APP_PEM.name})",
    )
    parser.add_argument(
        "--installation-id",
        type=int,
        default=None,
        help="GitHub App installation id (optional if --github-org can resolve it)",
    )
    parser.add_argument(
        "--list-installations",
        action="store_true",
        help="Print all GitHub App installations (id + account) and exit",
    )
    parser.add_argument(
        "--local-repos-dir",
        type=Path,
        help="Directory containing fully cloned Git repositories",
    )
    parser.add_argument(
        "--local-repo",
        action="append",
        default=[],
        help="Offline mode only: include matching local repo folder names (repeatable)",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Analyze --local-repos-dir only; make no network/API requests",
    )
    parser.add_argument(
        "--llm",
        action="store_true",
        help="Run opt-in OpenAI repository classification using temporary code samples",
    )
    parser.add_argument(
        "--llm-model",
        default=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
        help="OpenAI model for --llm mode (default: gpt-4o-mini)",
    )
    parser.add_argument(
        "--ui",
        action="store_true",
        help="Launch the local browser interface instead of an extraction",
    )
    parser.add_argument("--ui-host", default="127.0.0.1")
    parser.add_argument("--ui-port", type=int, default=8766)
    parser.add_argument("--github-org", action="append", default=[])
    parser.add_argument("--github-repo", action="append", default=[])
    parser.add_argument("--gitlab-group", action="append", default=[])
    parser.add_argument("--gitlab-project", action="append", default=[])
    args = parser.parse_args()

    if args.ui:
        from extract_ui import serve

        serve(host=args.ui_host, port=args.ui_port)
        return 0

    if args.offline and not args.local_repos_dir:
        raise SystemExit("--offline requires --local-repos-dir")
    if args.offline and (
        args.github_org
        or args.github_repo
        or args.gitlab_group
        or args.gitlab_project
    ):
        raise SystemExit("--offline cannot be combined with GitHub or GitLab targets")
    if args.llm and not os.environ.get("OPENAI_API_KEY", "").strip():
        raise SystemExit(
            "--llm requires OPENAI_API_KEY in the environment. "
            "Do not pass API keys as command-line arguments."
        )

    tokens: dict[str, str] = {}
    if args.tokens_file.exists():
        tokens = parse_tokens_file(args.tokens_file)
    elif not args.list_installations and not args.github_app and not args.offline:
        raise SystemExit(f"Tokens file not found: {args.tokens_file}")

    if args.list_installations:
        return cmd_list_installations(args, tokens)

    if not shutil.which("scc"):
        raise SystemExit("scc not found on PATH — install with: brew install scc")
    if not shutil.which("git"):
        raise SystemExit("git not found on PATH")

    github_token_fn: Callable[[], str] | None = None
    installation_id: int | None = None
    app_account = ""
    llm_config = (
        LLMConfig(
            api_key=os.environ["OPENAI_API_KEY"].strip(),
            model=args.llm_model,
        )
        if args.llm
        else None
    )
    if args.github_app or args.installation_id is not None:
        args.github_app = True
        github_token_fn, installation_id, app_account = build_github_app_token_fn(
            args, tokens
        )
        # Seed token for any code paths that still read the tokens map.
        tokens[GITHUB_APP_TOKEN_KEY] = github_token_fn()
        github_token_name = GITHUB_APP_TOKEN_KEY
    else:
        github_token_name = args.github_token_name

    if args.offline:
        targets = find_local_repositories(args.local_repos_dir.resolve())
        if args.local_repo:
            targets = filter_local_targets(targets, args.local_repo)
    else:
        targets = build_targets(
            args,
            tokens,
            github_token_name,
            args.gitlab_token_name,
            github_token_fn=github_token_fn,
        )
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if args.offline:
        label = args.local_repos_dir.name
    elif args.github_org:
        label = args.github_org[0]
    elif args.gitlab_group:
        label = args.gitlab_group[0]
    elif args.github_repo:
        label = args.github_repo[0].replace("/", "_")
    elif args.gitlab_project:
        label = args.gitlab_project[0].replace("/", "_")
    else:
        label = "repos"

    run_dir = args.output_dir / f"raw-extract-{safe_name(label)}-{stamp}"
    clones_dir = run_dir / "clones"
    logs_dir = run_dir / "logs"
    run_dir.mkdir(parents=True, exist_ok=True)
    clones_dir.mkdir(parents=True, exist_ok=True)

    log = setup_logger(logs_dir / "extract.log")
    log.info("Extracting %s repos → %s", len(targets), run_dir)
    if args.offline:
        log.info("Mode: offline local-clone analysis; no network requests will be made")
    elif args.github_app:
        log.info(
            "GitHub auth: app installation_id=%s account=%s",
            installation_id,
            app_account or "(unknown)",
        )
    else:
        log.info(
            "GitHub auth: PAT key=%s",
            args.github_token_name,
        )
    if not args.offline:
        log.info("GitLab token key=%s workers=%s", args.gitlab_token_name, args.workers)
    else:
        log.info("Workers=%s", args.workers)

    write_json(
        run_dir / "repos.json",
        [
            {"platform": t.platform, "org": t.org, "full_name": t.full_name}
            for t in targets
        ],
    )

    rows: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {
            pool.submit(
                process_repo,
                t,
                tokens=tokens,
                github_token_name=github_token_name,
                gitlab_token_name=args.gitlab_token_name,
                github_token_fn=github_token_fn,
                llm_config=llm_config,
                run_dir=run_dir,
                clones_dir=clones_dir,
                github_host=args.github_host,
                gitlab_host=args.gitlab_host,
                log=log,
            ): t
            for t in targets
        }
        for fut in as_completed(futures):
            rows.append(fut.result())

    rows.sort(key=lambda r: (str(r.get("org")), str(r.get("repo"))))
    summary_path = run_dir / "summary.csv"
    with summary_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=SUMMARY_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    shutil.rmtree(clones_dir, ignore_errors=True)

    manifest = {
        "created_at": stamp,
        "repos": len(targets),
        "ok": sum(1 for r in rows if not r.get("error")),
        "failed": sum(1 for r in rows if r.get("error")),
        "summary_csv": str(summary_path),
        "github_auth": (
            {
                "mode": "github_app",
                "installation_id": installation_id,
                "account": app_account,
            }
            if args.github_app
            else (
                {"mode": "offline"}
                if args.offline
                else {"mode": "pat", "token_name": args.github_token_name}
            )
        ),
        "llm": (
            {
                "enabled": True,
                "model": args.llm_model,
                "evidence_policy": (
                    "Temporary bounded README/file-path/source excerpts sent to OpenAI; "
                    "excerpts and API key are not stored in this archive."
                ),
            }
            if args.llm
            else {"enabled": False}
        ),
    }
    if args.offline:
        manifest["offline_field_sources"] = {
            "merged_prs": (
                "Detected from merge and squash markers in Git commit messages. "
                "Rebase merges and rewritten messages cannot be recovered."
            ),
            "contributor_count": "Unique Git author identities, including detected bots.",
            "repo_created_at": (
                "Timestamp of the root commit; not hosting-platform creation time."
            ),
            "primary_language": "Largest SCC language by code LOC.",
            "languages_breakdown": "SCC code LOC percentage by language.",
            "size_kb": (
                "Checked-out working-tree size excluding .git and common "
                "build/dependency directories."
            ),
        }
    write_json(run_dir / "manifest.json", manifest)

    zip_path = zip_run_dir(run_dir)
    log.info("Done. summary=%s", summary_path)
    log.info("Zip=%s", zip_path)
    print(
        json.dumps(
            {"run_dir": str(run_dir), "zip": str(zip_path), **manifest},
            indent=2,
        )
    )
    return 0 if manifest["failed"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
