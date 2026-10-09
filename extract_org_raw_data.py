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
  python extract_org_raw_data.py --github-org CustomerOrg --tokens-file tokens \\
      --github-token-name github-data-token --workers 10
  python extract_org_raw_data.py --gitlab-group my-group --tokens-file tokens --workers 8

  # Resume an interrupted run (skips repos already in summary.csv)
  python extract_org_raw_data.py --resume outputs/raw-extracts/raw-extract-ORG-STAMP

  # Retry only failed/retryable repos from a previous run
  python extract_org_raw_data.py --resume outputs/raw-extracts/raw-extract-ORG-STAMP --retry-failed
"""

from __future__ import annotations

import argparse
import base64
import csv
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from code_metrics import PR_FIELDS, STRUCTURE_FIELDS, pr_tier_metrics, structure_metrics
from count_merged_prs import (
    GITHUB_RATE_LIMIT_WAIT_SECONDS,
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
DEFAULT_BITBUCKET_TOKEN_NAME = "bitbucket_token"
DEFAULT_BITBUCKET_EMAIL_NAME = "bitbucket_email"
BITBUCKET_API = "https://api.bitbucket.org/2.0"
GITHUB_APP_TOKEN_KEY = "__github_app_installation_token__"
DEFAULT_CLONE_TIMEOUT_SECONDS = 300
DEFAULT_CLONE_RETRIES = 2

# ── large-repo cost controls ────────────────────────────────────────────────
# The per-commit file-churn export (commits.jsonl) is the single most expensive
# git step: it diffs every commit, which explodes on huge, binary-heavy histories
# (e.g. a 14k-commit Android repo read over a slow Docker bind mount). For repos
# past COMMIT_DETAIL_LIMIT commits we export per-file detail for only the most
# recent N and skip rename detection. total_commits, authors, dates and the
# merged-PR markers are unaffected — they come from cheaper full-history calls.
# All three are override-able via env for tuning without a code change.
#   EXTRACT_COMMIT_DETAIL_LIMIT  (0 = never cap; default 5000)
#   EXTRACT_GIT_LOG_TIMEOUT      seconds for git-log steps (default 900)
#   EXTRACT_SCC_TIMEOUT          seconds for scc (default 600)
def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


COMMIT_DETAIL_LIMIT = _env_int("EXTRACT_COMMIT_DETAIL_LIMIT", 0)
GIT_LOG_TIMEOUT_SECONDS = _env_int("EXTRACT_GIT_LOG_TIMEOUT", 900)
SCC_TIMEOUT_SECONDS = _env_int("EXTRACT_SCC_TIMEOUT", 600)
# Cloned repos are measured on the branch with the most recent commit, not the
# platform default (which is often a stale README-only stub while the work
# lives on develop/staging). EXTRACT_USE_DEFAULT_BRANCH=1 restores the old behaviour.
USE_DEFAULT_BRANCH = os.environ.get("EXTRACT_USE_DEFAULT_BRANCH", "").strip().lower() in {
    "1",
    "true",
    "yes",
}
RETRYABLE_ERROR_CLASSES = frozenset({"timeout", "rate_limit", "network"})

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

CLONE_CREDENTIAL_RE = re.compile(r"(https?://)(?:x-access-token|oauth2|x-token-auth):[^@/\s]+@", re.I)
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
    "project_name",
    "repo",
    "merged_prs",
    "languages_breakdown",
    "repo_created_at",
    "last_commit_date",
    "loc",
    "primary_language",
    "size_kb",
    "default_branch",
    "measured_branch",
    "total_commits",
    "first_commit",
    "span_days",
    "recency_days",
    "contributor_count",
    "human_authors",
    "bot_authors",
    "bot_commit_ratio",
    "total_files",
    "has_tests",
    "has_test_runner",
    "has_ci_cd",
    "test_source_loc_pct",
    "has_library_code",
    "open_source_loc_pct",
    "library_modules_loc_pct",
    *PR_FIELDS,
    *STRUCTURE_FIELDS,
    "company_period",
    "codebase_description",
    "industry_domain",
    "vibe_code_signals",
    "repo_type",
    "llm_analysis_error",
    "error",
    "error_class",
]

TEST_DIR_HINTS = ("test", "tests", "spec", "specs", "__tests__")
SKIP_WALK_DIRS = {
    ".git",
    "node_modules",
    "vendor",
    "dist",
    "build",
    ".venv",
    "venv",
    "bower_components",
    "__pycache__",
    ".tox",
    "Pods",
    "Carthage",
    ".gradle",
    "target",
    "bin",
    "obj",
    "packages",
}
LOCAL_PLATFORM_ROOTS = frozenset({"github", "gitlab", "bitbucket", "local"})
LLM_SEMAPHORE = threading.BoundedSemaphore(3)
LLM_SOURCE_EXTENSIONS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".go", ".rs", ".rb", ".php",
    ".cs", ".swift", ".kt", ".scala", ".vue", ".svelte",
}
CODE_LOC_EXTENSIONS = LLM_SOURCE_EXTENSIONS | {
    ".c", ".cc", ".cpp", ".h", ".hpp", ".m", ".mm", ".cshtml", ".aspx",
    ".jsp", ".vue", ".svelte", ".dart", ".groovy", ".kts", ".fs", ".fsx",
}
CI_FILE_NAMES = frozenset(
    {
        ".gitlab-ci.yml",
        ".travis.yml",
        ".drone.yml",
        ".woodpecker.yml",
        "jenkinsfile",
        "azure-pipelines.yml",
        "azure-pipelines.yaml",
        "bitbucket-pipelines.yml",
        "cloudbuild.yaml",
        "cloudbuild.yml",
        "buildkite.yml",
        "codebuild.yml",
    }
)
CI_DIR_HINTS = (
    (".github", "workflows"),
    (".circleci",),
    (".buildkite",),
    (".woodpecker",),
)
TEST_RUNNER_FILE_NAMES = frozenset(
    {
        "jest.config.js",
        "jest.config.cjs",
        "jest.config.mjs",
        "jest.config.ts",
        "vitest.config.js",
        "vitest.config.ts",
        "vitest.config.mjs",
        "karma.conf.js",
        "karma.conf.ts",
        "mocha.opts",
        ".mocharc.js",
        ".mocharc.cjs",
        ".mocharc.json",
        ".mocharc.yml",
        "ava.config.js",
        "ava.config.cjs",
        "pytest.ini",
        "phpunit.xml",
        "phpunit.xml.dist",
        "tox.ini",
        ".rspec",
        "nose2.cfg",
        "conftest.py",
    }
)
TEST_RUNNER_PACKAGE_PATTERNS = [
    re.compile(p, re.I)
    for p in [
        r"\bjest\b",
        r"\bvitest\b",
        r"\bmocha\b",
        r"\bava\b",
        r"\bpytest\b",
        r"\bphpunit\b",
        r"\bnunit\b",
        r"\bxunit\b",
        r"\bmstest\b",
        r"Microsoft\.NET\.Test\.Sdk",
        r"Microsoft\.VisualStudio\.Test",
        r"go test",
    ]
]
LIBRARY_DIR_NAMES = frozenset({"lib", "libs", "library", "libraries"})
# Library/framework/module path segments for LOC % (compiled sheets).
LIBRARY_MODULE_DIR_NAMES = frozenset(
    {
        "node_modules",
        "bower_components",
        ".venv",
        "venv",
        "site-packages",
        "lib",
        "libs",
        "library",
        "libraries",
        "modules",
        "framework",
        "frameworks",
        "packages",
    }
)
# Strong open-source signals (not bare folder-name heuristics).
LICENSE_FILE_NAMES = frozenset(
    {
        "license",
        "license.md",
        "license.txt",
        "license.rst",
        "copying",
        "copying.md",
        "copying.txt",
        "licence",
        "licence.md",
        "licence.txt",
    }
)
OSS_LICENSE_MARKERS = (
    re.compile(r"\bMIT\b"),
    re.compile(r"\bApache(?:\s+License)?(?:\s*,?\s*version\s*)?2(\.0)?\b", re.I),
    re.compile(r"\bBSD[- ][23][- ]Clause\b", re.I),
    re.compile(r"\bBSD\b"),
    re.compile(r"\bISC\b"),
    re.compile(r"\bMPL(?:-|\s+)2(\.0)?\b", re.I),
    re.compile(r"\bGNU\s+(Lesser\s+|Affero\s+)?General\s+Public\s+License\b", re.I),
    re.compile(r"\b(?:LGPL|AGPL|GPL)-?[23](?:\.0)?\b", re.I),
    re.compile(r"\bUnlicense\b", re.I),
    re.compile(r"\bCC0\b", re.I),
    re.compile(r"\bBoost\s+Software\s+License\b", re.I),
    re.compile(r"\bArtistic\s+License\b", re.I),
    re.compile(r"\bZlib\b", re.I),
    re.compile(r"\bEclipse\s+Public\s+License\b", re.I),
    re.compile(r"\bMozilla\s+Public\s+License\b", re.I),
)
PROPRIETARY_LICENSE_MARKERS = (
    re.compile(r"\ball\s+rights\s+reserved\b", re.I),
    re.compile(r"\bproprietary\b", re.I),
    re.compile(r"\bconfidential\b", re.I),
    re.compile(r"\bnot\s+licensed\s+for\b", re.I),
)
SPDX_LINE_RE = re.compile(
    r"SPDX-License-Identifier\s*:\s*([^\n*;]+)",
    re.I,
)
OSS_SPDX_IDS = frozenset(
    {
        "mit",
        "apache-2.0",
        "bsd-2-clause",
        "bsd-3-clause",
        "isc",
        "mpl-2.0",
        "gpl-2.0",
        "gpl-3.0",
        "gpl-2.0-only",
        "gpl-2.0-or-later",
        "gpl-3.0-only",
        "gpl-3.0-or-later",
        "lgpl-2.1",
        "lgpl-3.0",
        "agpl-3.0",
        "unlicense",
        "cc0-1.0",
        "bsl-1.0",
        "zlib",
        "epl-2.0",
        "0bsd",
        "openssl",
    }
)
# (dependency dir name, required sibling/root marker files) — marker proves PM install.
LOCKFILE_BACKED_DEP_DIRS: tuple[tuple[str, frozenset[str]], ...] = (
    ("vendor", frozenset({"composer.lock", "composer.json", "go.mod", "go.sum"})),
    (
        "node_modules",
        frozenset(
            {
                "package-lock.json",
                "yarn.lock",
                "pnpm-lock.yaml",
                "npm-shrinkwrap.json",
                "package.json",
            }
        ),
    ),
    ("pods", frozenset({"podfile.lock", "podfile"})),
    ("bower_components", frozenset({"bower.json"})),
)
CARTHAGE_MARKERS = frozenset({"cartfile.resolved", "cartfile"})


@dataclass(frozen=True)
class LLMConfig:
    api_key: str
    model: str


@dataclass
class RepoTarget:
    platform: str  # github | gitlab | bitbucket | local
    org: str
    full_name: str
    meta: dict[str, Any]
    local_path: Path | None = None


def parse_tokens_file(path: Path) -> dict[str, str]:
    if path.is_dir():
        raise ValueError(
            f"Tokens path is a directory, expected a file: {path}. "
            "If Docker created this after a missing bind mount, remove the "
            "directory on the host and copy tokens.example to tokens."
        )
    if not path.is_file():
        raise ValueError(f"Tokens file not found: {path}")
    # The checks above can pass and the read below still fail: a Docker
    # single-file bind mount keeps serving cached stat() attributes after the
    # host file is replaced, so open() raises ENOENT for a path that "exists".
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(
            f"Tokens file could not be read: {path} ({exc.strerror}). "
            "If this is the Docker container, the bind mount for the tokens "
            "file has gone stale — run 'docker compose down && docker compose "
            "up -d' on the host and try again."
        ) from exc
    tokens: dict[str, str] = {}
    for line in raw.splitlines():
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
    level_name = os.environ.get("EXTRACT_LOG_LEVEL", "INFO").strip().upper()
    level = getattr(logging, level_name, logging.INFO)
    logger.setLevel(level)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger


def _tool_version(command: list[str]) -> str:
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=10,
        )
        text = (proc.stdout or proc.stderr or "").strip().splitlines()
        return text[0] if text else f"exit={proc.returncode}"
    except Exception as exc:
        return f"unavailable ({exc})"


def log_runtime_diagnostics(log: logging.Logger, args: argparse.Namespace) -> None:
    """Write environment details useful for support (never includes secrets)."""
    log.info("=== Runtime diagnostics ===")
    log.info("python=%s", sys.version.replace("\n", " "))
    log.info("platform=%s", sys.platform)
    log.info("cwd=%s", Path.cwd())
    log.info("extractor=%s", Path(__file__).resolve())
    log.info("git=%s", _tool_version(["git", "--version"]))
    log.info("scc=%s", _tool_version(["scc", "--version"]))
    log.info("workers=%s", args.workers)
    log.info("clone_timeout=%s", getattr(args, "clone_timeout", DEFAULT_CLONE_TIMEOUT_SECONDS))
    log.info("clone_retries=%s", getattr(args, "clone_retries", DEFAULT_CLONE_RETRIES))
    log.info("github_rate_limit_wait_seconds=%s", GITHUB_RATE_LIMIT_WAIT_SECONDS)
    log.info("resume=%s", getattr(args, "resume", None))
    log.info("offline=%s", bool(args.offline))
    log.info("llm=%s", bool(args.llm))
    if args.offline:
        log.info("local_repos_dir=%s", args.local_repos_dir)
    else:
        log.info("github_host=%s", args.github_host)
        log.info("gitlab_host=%s", args.gitlab_host)
        log.info("github_token_name=%s", args.github_token_name)
        log.info("gitlab_token_name=%s", args.gitlab_token_name)
        log.info("github_org=%s", args.github_org or [])
        log.info("github_accessible=%s", bool(args.github_accessible))
        log.info("gitlab_group=%s", args.gitlab_group or [])
        log.info("gitlab_accessible=%s", bool(args.gitlab_accessible))
        log.info("bitbucket_token_name=%s", args.bitbucket_token_name)
        log.info("bitbucket_workspace=%s", args.bitbucket_workspace or [])
        log.info("bitbucket_repo_count=%s", len(args.bitbucket_repo or []))
        log.info(
            "github_repo_count=%s gitlab_repo_count=%s",
            len(args.github_repo or []),
            len(args.gitlab_repo or []),
        )
    log.info("output_dir=%s", args.output_dir)
    log.info("=== End diagnostics ===")


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def classify_error(exc: BaseException) -> str:
    """Map an exception to a stable error class for skip/retry decisions."""
    if isinstance(exc, subprocess.TimeoutExpired):
        return "timeout"
    text = str(exc).lower()
    if "timed out" in text or "timeout" in text:
        return "timeout"
    if "429" in text or "rate limit" in text or "secondary rate" in text:
        return "rate_limit"
    if any(
        token in text
        for token in (
            "401",
            "403",
            "authentication",
            "permission denied",
            "access denied",
            "bad credentials",
            "invalid token",
        )
    ):
        return "auth"
    if "404" in text or "not found" in text or "does not exist" in text:
        return "not_found"
    if any(
        token in text
        for token in (
            "no space left",
            "disk quota",
            "not enough space",
            "errno 28",
        )
    ):
        return "disk_full"
    if any(
        token in text
        for token in (
            "network",
            "connection",
            "could not resolve",
            "temporary failure",
            "tls",
            "ssl",
            "broken pipe",
            "connection reset",
            "unavailable",
        )
    ):
        return "network"
    return "unknown"


def is_retryable_error_class(error_class: str) -> bool:
    return error_class in RETRYABLE_ERROR_CLASSES


def repo_row_key(org: str, repo: str) -> str:
    return f"{org}/{repo}".strip("/")


@dataclass
class JobCheckpoint:
    """Persist per-repo progress so interrupted runs can resume."""

    run_dir: Path
    total: int = 0
    created_at: str = ""
    updated_at: str = ""
    status_by_key: dict[str, dict[str, Any]] = field(default_factory=dict)
    rows_by_key: dict[str, dict[str, Any]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def job_path(self) -> Path:
        return self.run_dir / "job.json"

    @property
    def summary_path(self) -> Path:
        return self.run_dir / "summary.csv"

    @classmethod
    def load_or_create(cls, run_dir: Path, total: int) -> "JobCheckpoint":
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        job = cls(run_dir=run_dir, total=total, created_at=stamp, updated_at=stamp)
        if job.job_path.is_file():
            try:
                data = json.loads(job.job_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = {}
            if isinstance(data, dict):
                job.created_at = str(data.get("created_at") or stamp)
                job.total = int(data.get("total") or total)
                statuses = data.get("repos") or {}
                if isinstance(statuses, dict):
                    job.status_by_key = {
                        str(key): value
                        for key, value in statuses.items()
                        if isinstance(value, dict)
                    }
        if job.summary_path.is_file():
            with job.summary_path.open(newline="", encoding="utf-8") as handle:
                for row in csv.DictReader(handle):
                    org = str(row.get("org") or "")
                    repo = str(row.get("repo") or "")
                    if not org or not repo:
                        continue
                    key = repo_row_key(org, repo)
                    job.rows_by_key[key] = dict(row)
                    if key not in job.status_by_key:
                        error = str(row.get("error") or "").strip()
                        error_class = str(row.get("error_class") or "").strip()
                        if not error_class and error:
                            error_class = "unknown"
                        job.status_by_key[key] = {
                            "status": "failed" if error else "ok",
                            "error_class": error_class,
                            "updated_at": stamp,
                        }
        job.persist()
        return job

    def completed_ok_keys(self) -> set[str]:
        return {
            key
            for key, meta in self.status_by_key.items()
            if meta.get("status") == "ok"
        }

    def failed_keys(self, *, retryable_only: bool = False) -> set[str]:
        failed: set[str] = set()
        for key, meta in self.status_by_key.items():
            if meta.get("status") != "failed":
                continue
            error_class = str(meta.get("error_class") or "unknown")
            if retryable_only and not is_retryable_error_class(error_class):
                continue
            failed.add(key)
        return failed

    def record(self, full_name: str, row: dict[str, Any]) -> None:
        key = full_name.strip("/")
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        error = str(row.get("error") or "").strip()
        error_class = str(row.get("error_class") or "").strip()
        status = "failed" if error else "ok"
        with self._lock:
            self.rows_by_key[key] = dict(row)
            self.status_by_key[key] = {
                "status": status,
                "error_class": error_class,
                "updated_at": stamp,
            }
            self.updated_at = stamp
            self._write_summary_unlocked()
            self._persist_unlocked()

    def rows(self) -> list[dict[str, Any]]:
        with self._lock:
            values = list(self.rows_by_key.values())
        values.sort(key=lambda r: (str(r.get("org")), str(r.get("repo"))))
        return values

    def persist(self) -> None:
        with self._lock:
            self._persist_unlocked()

    def write_summary(self) -> None:
        with self._lock:
            self._write_summary_unlocked()

    def replace_rows(self, rows: list[dict[str, Any]]) -> None:
        with self._lock:
            self.rows_by_key = {}
            for row in rows:
                key = repo_row_key(str(row.get("org") or ""), str(row.get("repo") or ""))
                if key:
                    self.rows_by_key[key] = dict(row)
            self.updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            self._write_summary_unlocked()
            self._persist_unlocked()

    def progress_snapshot(self) -> dict[str, Any]:
        with self._lock:
            ok = sum(1 for m in self.status_by_key.values() if m.get("status") == "ok")
            failed = sum(
                1 for m in self.status_by_key.values() if m.get("status") == "failed"
            )
            by_class: dict[str, int] = {}
            for meta in self.status_by_key.values():
                if meta.get("status") != "failed":
                    continue
                error_class = str(meta.get("error_class") or "unknown")
                by_class[error_class] = by_class.get(error_class, 0) + 1
            return {
                "total": self.total,
                "ok": ok,
                "failed": failed,
                "done": ok + failed,
                "error_classes": by_class,
            }

    def _persist_unlocked(self) -> None:
        payload = {
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "total": self.total,
            "progress": {
                "ok": sum(
                    1 for m in self.status_by_key.values() if m.get("status") == "ok"
                ),
                "failed": sum(
                    1
                    for m in self.status_by_key.values()
                    if m.get("status") == "failed"
                ),
                "done": len(self.status_by_key),
            },
            "repos": self.status_by_key,
        }
        write_json(self.job_path, payload)

    def _write_summary_unlocked(self) -> None:
        rows = list(self.rows_by_key.values())
        rows.sort(key=lambda r: (str(r.get("org")), str(r.get("repo"))))
        with self.summary_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=SUMMARY_FIELDS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)


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
        try:
            batch = paginate_github(f"{api}/{kind}?per_page=100&type=all", token)
        except RuntimeError as exc:
            if "HTTP 404" not in str(exc):
                raise
            continue
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
        {"include_subgroups": "true"},
    )
    if not projects:
        raise RuntimeError(f"No GitLab projects found for group {group!r}")
    return projects


def list_gitlab_accessible_project_objects(
    token: str, host: str = "gitlab.com"
) -> list[dict[str, Any]]:
    """Projects the token can access via membership (any group or personal namespace)."""
    api = gitlab_api(host)
    projects = paginate_gitlab(
        api,
        "/projects",
        token,
        {
            "membership": "true",
            "order_by": "path",
            "sort": "asc",
        },
    )
    results = [
        project
        for project in projects
        if isinstance(project, dict) and project.get("path_with_namespace")
    ]
    if not results:
        raise RuntimeError(
            "No GitLab projects found for this token (membership access)."
        )
    return results


def fetch_gitlab_project(token: str, path_with_namespace: str, host: str) -> dict[str, Any]:
    api = gitlab_api(host)
    encoded = urllib.parse.quote(path_with_namespace.strip("/"), safe="")
    data, _ = http_get_json(
        f"{api}/projects/{encoded}",
        {"PRIVATE-TOKEN": token, "User-Agent": "extract-org-raw-data"},
    )
    if not isinstance(data, dict) or not data.get("path_with_namespace"):
        raise RuntimeError(f"GitLab project not found: {path_with_namespace}")
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
        # Linked issues feed the "rich" PR tier. Keep only identifiers: issue
        # titles and bodies are not needed and do not belong in the archive.
        try:
            closes = paginate_gitlab(
                api,
                f"/projects/{project_id}/merge_requests/{iid}/closes_issues",
                token,
                {"per_page": "100"},
            )
            detail["closes_issues"] = [
                {"iid": i.get("iid"), "web_url": i.get("web_url")} for i in closes
            ]
        except Exception:
            detail["closes_issues"] = []
        # Deliberately NOT fetching /merge_requests/:iid/changes. Each entry in
        # that response carries a `diff` field holding the actual source, which
        # landed in merged_prs.json and shipped inside the deliverable zip --
        # breaking the metadata-only guarantee this tool is built on. The size
        # signal it was providing is already in `changes_count` on the detail
        # above, at no extra request.
        enriched.append(detail)
    return enriched


# ── Bitbucket Cloud ─────────────────────────────────────────────────────────
#
# Bitbucket Cloud has no account-wide discovery (``/workspaces`` is gone and
# ``/repositories`` without a workspace is 410), so a workspace slug is always
# required. REST and git authenticate differently: REST takes ``email:token``
# Basic auth (or a Bearer token when no email is configured, as for workspace
# and repository access tokens); git over HTTPS needs the literal username
# ``x-token-auth``.


def bitbucket_headers(token: str, email: str = "") -> dict[str, str]:
    if email:
        raw = base64.b64encode(f"{email}:{token}".encode("utf-8")).decode("ascii")
        auth = f"Basic {raw}"
    else:
        auth = f"Bearer {token}"
    return {
        "Authorization": auth,
        "Accept": "application/json",
        "User-Agent": "extract-org-raw-data",
    }


def paginate_bitbucket(
    url: str, token: str, email: str = "", *, max_pages: int = 500
) -> list[Any]:
    """Follow Bitbucket's ``next`` links and return every ``values`` item."""
    items: list[Any] = []
    headers = bitbucket_headers(token, email)
    next_url: str | None = url
    for _ in range(max_pages):
        if not next_url:
            break
        data, _hdrs = http_get_json(next_url, headers)
        if not isinstance(data, dict):
            break
        items.extend(data.get("values") or [])
        next_url = data.get("next")
    return items


def list_bitbucket_repo_objects(
    token: str, workspace: str, email: str = ""
) -> list[dict[str, Any]]:
    ws = urllib.parse.quote(workspace.strip("/"), safe="")
    repos = paginate_bitbucket(
        f"{BITBUCKET_API}/repositories/{ws}?pagelen=100&sort=slug", token, email
    )
    repos = [r for r in repos if isinstance(r, dict) and r.get("full_name")]
    if not repos:
        raise RuntimeError(
            f"No Bitbucket repositories found for workspace {workspace!r} "
            "(check the slug and that the token has repository:read)"
        )
    return repos


def fetch_bitbucket_repo(token: str, full_name: str, email: str = "") -> dict[str, Any]:
    data, _ = http_get_json(
        f"{BITBUCKET_API}/repositories/{full_name.strip('/')}",
        bitbucket_headers(token, email),
    )
    if not isinstance(data, dict) or not data.get("full_name"):
        raise RuntimeError(f"Bitbucket repository not found: {full_name}")
    return data


def fetch_bitbucket_merged_prs(
    token: str, full_name: str, email: str = ""
) -> list[dict[str, Any]]:
    prs = paginate_bitbucket(
        f"{BITBUCKET_API}/repositories/{full_name}/pullrequests"
        "?state=MERGED&pagelen=50",
        token,
        email,
    )
    for pr in prs:
        author = pr.get("author") or {}
        login = author.get("nickname") or author.get("display_name") or ""
        pr["author_is_bot"] = is_bot_login(login)
    return prs


# ── git / scc ───────────────────────────────────────────────────────────────


def run_git(
    repo: Path,
    *args: str,
    timeout: int = 300,
    log: logging.Logger | None = None,
) -> str:
    """Run a git command in ``repo``.

    Always sets ``safe.directory=*`` so host-mounted clones remain readable inside
    Docker when UID/GID ownership does not match the container user.
    """
    proc = subprocess.run(
        [
            "git",
            "-c",
            "safe.directory=*",
            "-C",
            str(repo),
            *args,
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        if log is not None:
            detail = (proc.stderr or proc.stdout or "git failed").strip()
            log.warning("git %s failed in %s: %s", " ".join(args), repo, detail)
        return ""
    return proc.stdout


def probe_local_git(repo: Path, log: logging.Logger) -> bool:
    """Return True when git history is readable; log a clear warning otherwise."""
    proc = subprocess.run(
        [
            "git",
            "-c",
            "safe.directory=*",
            "-C",
            str(repo),
            "rev-list",
            "--count",
            "HEAD",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode == 0 and proc.stdout.strip().isdigit():
        return True
    detail = (proc.stderr or proc.stdout or "git failed").strip()
    log.warning(
        "Local git history unreadable at %s (%s). "
        "If you see 'dubious ownership', the container cannot trust this mount; "
        "this build marks safe.directory=* automatically — rebuild/restart the image, "
        "or run: git config --global --add safe.directory '*'",
        repo,
        detail or "unknown error",
    )
    return False


def clone_repo(
    url: str,
    dest: Path,
    timeout: int = DEFAULT_CLONE_TIMEOUT_SECONDS,
    retries: int = DEFAULT_CLONE_RETRIES,
    log: logging.Logger | None = None,
) -> None:
    """Clone with a short timeout and a few retries, then move on to the next repo."""
    attempts = max(1, retries + 1)
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        dest.parent.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, "GIT_LFS_SKIP_SMUDGE": "1"}
        try:
            proc = subprocess.run(
                ["git", "clone", "--recurse-submodules=0", url, str(dest)],
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            shutil.rmtree(dest, ignore_errors=True)
            last_error = RuntimeError(
                f"clone timed out after {timeout}s (attempt {attempt}/{attempts})"
            )
            if log is not None:
                log.warning("%s", last_error)
            if attempt >= attempts:
                raise last_error from exc
            time.sleep(min(2**attempt, 20))
            continue
        if proc.returncode == 0:
            return
        detail = proc.stderr or proc.stdout or "clone failed"
        detail = CLONE_CREDENTIAL_RE.sub(r"\1***:***@", detail)
        try:
            shutil.rmtree(dest)
        except OSError as exc:
            # Log cleanup failure but don't suppress - will be caught in process_repo()
            log.warning("Cleanup failed after clone error (disk may be full): %s", exc)
        last_error = RuntimeError(detail[-800:])
        error_class = classify_error(last_error)
        # Auth / not-found will not improve with retries.
        if error_class in {"auth", "not_found", "disk_full"} or attempt >= attempts:
            raise last_error
        if log is not None:
            log.warning(
                "clone failed (%s) attempt %s/%s; retrying: %s",
                error_class,
                attempt,
                attempts,
                str(last_error)[:200],
            )
        time.sleep(min(2**attempt, 20))
    assert last_error is not None
    raise last_error


def github_clone_url(full_name: str, token: str, host: str) -> str:
    if host == "github.com":
        return f"https://x-access-token:{token}@github.com/{full_name}.git"
    return f"https://x-access-token:{token}@{host}/{full_name}.git"


def gitlab_clone_url(full_name: str, token: str, host: str) -> str:
    host = host.rstrip("/")
    scheme = "http://" if host.startswith("http://") else "https://"
    bare = host.replace("https://", "").replace("http://", "")
    return f"{scheme}oauth2:{token}@{bare}/{full_name}.git"


def bitbucket_clone_url(full_name: str, token: str) -> str:
    return f"https://x-token-auth:{token}@bitbucket.org/{full_name}.git"


def select_latest_branch(
    repo: Path, default_branch: str = "", log: logging.Logger | None = None
) -> str:
    """Check out the remote branch with the newest commit; return its name.

    The clone checks out only the platform default, which can be a stale stub
    while the real history sits on other branches. LOC, language, commit and
    author stats are all read from HEAD, so moving HEAD fixes them together.
    Ties go to the default branch. Returns "" if nothing could be selected, in
    which case HEAD is left untouched.
    """
    prefix = "refs/remotes/origin/"
    try:
        out = run_git(
            repo,
            "for-each-ref",
            "--sort=-committerdate",
            "--format=%(refname)\t%(committerdate:unix)",
            prefix,
        )
    except Exception as exc:  # noqa: BLE001 - never fail a repo over branch choice
        if log is not None:
            log.warning("branch selection skipped (for-each-ref failed): %s", exc)
        return ""
    candidates: list[tuple[int, str]] = []
    for line in out.splitlines():
        ref, _, ts = line.partition("\t")
        name = ref[len(prefix):] if ref.startswith(prefix) else ""
        if not name or name == "HEAD" or not ts.strip().isdigit():
            continue
        candidates.append((int(ts), name))
    if not candidates:
        return ""
    newest = max(ts for ts, _ in candidates)
    tied = [name for ts, name in candidates if ts == newest]
    chosen = default_branch if default_branch in tied else tied[0]
    try:
        current = run_git(repo, "symbolic-ref", "--short", "HEAD").strip()
    except Exception:  # noqa: BLE001 - detached HEAD
        current = ""
    if chosen != current:
        try:
            proc = subprocess.run(
                ["git", "-c", "safe.directory=*", "-C", str(repo), "checkout", "-q",
                 "-B", chosen, f"origin/{chosen}"],
                capture_output=True,
                text=True,
                timeout=GIT_LOG_TIMEOUT_SECONDS,
                env={**os.environ, "GIT_LFS_SKIP_SMUDGE": "1"},
            )
            if proc.returncode != 0:
                raise RuntimeError((proc.stderr or proc.stdout or "checkout failed")[-300:])
        except Exception as exc:  # noqa: BLE001
            if log is not None:
                log.warning("could not check out %s, staying on %s: %s", chosen, current, exc)
            return current
        if log is not None:
            log.info("measuring branch %s (default %s)", chosen, default_branch or current)
    return chosen


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


def export_commits_jsonl(
    repo: Path,
    out_path: Path,
    *,
    total_commits: int | None = None,
    max_commits: int = COMMIT_DETAIL_LIMIT,
    timeout: int = GIT_LOG_TIMEOUT_SECONDS,
    log: logging.Logger | None = None,
) -> int:
    """Write per-commit file churn as JSONL.

    For very large histories, cap the *detailed* export to the most recent
    ``max_commits`` commits and skip rename detection — the two changes that make
    a huge, binary-heavy repo finish cheaply. Full totals/authors/dates come from
    ``aggregate_git_stats`` and are unaffected; a sibling ``commits_detail_meta``
    records the cap so nothing is silently truncated.
    """
    pretty = "--pretty=format:COMMIT\t%H\t%aN\t%aE\t%aI\t%s"
    capped = False
    if max_commits and max_commits > 0:
        if total_commits is None:
            counted = run_git(repo, "rev-list", "--count", "HEAD", log=log).strip()
            total_commits = int(counted) if counted.isdigit() else 0
        if total_commits and total_commits > max_commits:
            capped = True
    if capped:
        log_args = ["log", f"-n{max_commits}", "--no-renames", pretty, "--numstat"]
    else:
        log_args = ["log", pretty, "--numstat"]
    text = run_git(repo, *log_args, timeout=timeout, log=log)
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
    if capped:
        if log is not None:
            log.warning(
                "commits.jsonl: exported per-file detail for the most recent %s of "
                "%s commits (large history, cost cap); totals/authors/dates are complete.",
                max_commits,
                total_commits,
            )
        write_json(
            out_path.parent / "commits_detail_meta.json",
            {
                "capped": True,
                "total_commits": total_commits,
                "detail_commits_exported": count,
                "detail_limit": max_commits,
                "rename_detection": False,
                "note": (
                    "Per-commit file churn was limited to the most recent commits to "
                    "keep cost bounded on a large history. total_commits, authors, "
                    "dates and merged-PR markers reflect the FULL history."
                ),
            },
        )
    return count


def run_scc(repo: Path) -> dict[str, Any]:
    if not shutil.which("scc"):
        raise RuntimeError("scc not found on PATH")
    proc = subprocess.run(
        ["scc", "--format", "json", str(repo)],
        capture_output=True,
        text=True,
        timeout=SCC_TIMEOUT_SECONDS,
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
        timeout=GIT_LOG_TIMEOUT_SECONDS,
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
    # Numbered markers alone undercount: merge commits without a PR/MR number in
    # the message (plain ``git merge``, Bitbucket UI merges with edited subjects)
    # are still merged work, so add every merge commit not already counted.
    counted = {record["merge_commit"] for record in records.values()}
    extra = [
        merge
        for merge in unnumbered_merge_commits(repo)
        if merge["merge_commit"] not in counted
    ]
    return sorted(
        [*records.values(), *extra], key=lambda record: record["merged_at"]
    )


def unnumbered_merge_commits(repo: Path) -> list[dict[str, str]]:
    """Merge commits without PR/MR numbers in their message.

    Legacy GitLab repositories often merge branches directly, so the only
    evidence of a review cycle is the merge commit itself.
    """
    text = run_git(
        repo,
        "log",
        "--merges",
        "--pretty=format:%H%x1f%aI%x1f%an%x1f%ae%x1f%s%x1e",
        timeout=600,
    )
    records: list[dict[str, str]] = []
    for raw_record in text.split("\x1e"):
        fields = raw_record.strip("\n").split("\x1f")
        if len(fields) < 5:
            continue
        sha, date, author_name, author_email, subject = fields[:5]
        records.append(
            {
                "pr_number": "",
                "method": "merge-commit-unnumbered",
                "source_branch": "",
                "title": subject,
                "merged_at": date,
                "merged_by_name": author_name,
                "merged_by_email": author_email,
                "merge_commit": sha,
            }
        )
    return sorted(records, key=lambda record: record["merged_at"])




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


def _path_is_test(relative: str) -> bool:
    lowered = relative.lower().replace("\\", "/")
    parts = lowered.split("/")
    if any(part in TEST_DIR_HINTS for part in parts):
        return True
    name = parts[-1] if parts else lowered
    return (
        name.startswith("test_")
        or name.endswith("_test.py")
        or name.endswith("_test.go")
        or name.endswith("_test.rb")
        or name.endswith(".spec.ts")
        or name.endswith(".spec.tsx")
        or name.endswith(".spec.js")
        or name.endswith(".spec.jsx")
        or name.endswith(".test.ts")
        or name.endswith(".test.tsx")
        or name.endswith(".test.js")
        or name.endswith(".test.jsx")
        or name.endswith("tests.cs")
        or name.endswith("test.cs")
        or name.endswith("test.java")
    )


def count_total_files(repo: Path) -> int:
    """Count files excluding .git and common dependency/build module directories."""
    total = 0
    for root, dirs, files in os.walk(repo):
        dirs[:] = [directory for directory in dirs if directory not in SKIP_WALK_DIRS]
        total += len(files)
    return total


def detect_ci_cd(repo: Path) -> bool:
    """True when common CI/CD config files or workflow directories are present."""
    for dir_parts in CI_DIR_HINTS:
        if (repo.joinpath(*dir_parts)).is_dir():
            return True
    for root, dirs, files in os.walk(repo):
        dirs[:] = [directory for directory in dirs if directory not in SKIP_WALK_DIRS]
        for filename in files:
            if filename.lower() in CI_FILE_NAMES:
                return True
            if filename.lower().startswith("jenkinsfile"):
                return True
    return False


def _text_has_test_runner_marker(text: str) -> bool:
    return any(pattern.search(text) for pattern in TEST_RUNNER_PACKAGE_PATTERNS)


def detect_test_runner(repo: Path) -> bool:
    """True when test-runner config or project SDK references are present."""
    for root, dirs, files in os.walk(repo):
        dirs[:] = [directory for directory in dirs if directory not in SKIP_WALK_DIRS]
        for filename in files:
            path = Path(root) / filename
            name = filename.lower()
            if name in TEST_RUNNER_FILE_NAMES:
                return True
            if name.endswith(".csproj"):
                if _text_has_test_runner_marker(_read_text_sample(path, 20_000)):
                    return True
            if name in {
                "package.json",
                "composer.json",
                "pyproject.toml",
                "pom.xml",
                "setup.cfg",
            }:
                if _text_has_test_runner_marker(_read_text_sample(path, 40_000)):
                    return True
            if name in {"build.gradle", "build.gradle.kts"}:
                sample = _read_text_sample(path, 40_000).lower()
                if "junit" in sample or "testimplementation" in sample or "usebjunit" in sample:
                    return True
    return False


def _count_file_lines(path: Path) -> int:
    try:
        with path.open("rb") as fh:
            return sum(1 for _ in fh)
    except OSError:
        return 0


def test_source_loc_pct(repo: Path) -> float | str:
    """Static test:source LOC ratio as a percentage (not executed coverage)."""
    test_loc = 0
    source_loc = 0
    for root, dirs, files in os.walk(repo):
        dirs[:] = [directory for directory in dirs if directory not in SKIP_WALK_DIRS]
        for filename in files:
            path = Path(root) / filename
            if path.suffix.lower() not in CODE_LOC_EXTENSIONS:
                continue
            relative = path.relative_to(repo).as_posix()
            lines = _count_file_lines(path)
            if _path_is_test(relative):
                test_loc += lines
            else:
                source_loc += lines
    if source_loc <= 0:
        return ""
    return round(100.0 * test_loc / source_loc, 1)


def _read_text_head(path: Path, limit: int = 12_000) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as fh:
            return fh.read(limit)
    except OSError:
        return ""


def _license_text_is_oss(text: str) -> bool:
    if not text.strip():
        return False
    has_oss = any(marker.search(text) for marker in OSS_LICENSE_MARKERS)
    if not has_oss:
        return False
    # Reject proprietary wrappers that only mention OSS in passing.
    proprietary_hits = sum(1 for marker in PROPRIETARY_LICENSE_MARKERS if marker.search(text))
    if proprietary_hits and not re.search(
        r"\b(?:licensed\s+under|released\s+under|permission\s+is\s+hereby\s+granted)\b",
        text,
        re.I,
    ):
        return False
    return True


def _spdx_ids_are_oss(spdx_value: str) -> bool:
    # Support simple expressions: "MIT", "Apache-2.0 OR MIT"
    tokens = re.split(r"[\s()]+", spdx_value.strip().lower())
    ids = [t for t in tokens if t and t not in {"or", "and", "with"}]
    if not ids:
        return False
    return all(token in OSS_SPDX_IDS for token in ids)


def _file_has_oss_spdx(path: Path) -> bool:
    head = _read_text_head(path, 4_096)
    match = SPDX_LINE_RE.search(head)
    if not match:
        return False
    return _spdx_ids_are_oss(match.group(1))


def _collect_lockfile_backed_dep_roots(repo: Path) -> list[str]:
    """Return repo-relative directory prefixes proven by package-manager lock/manifests."""
    roots: list[str] = []
    repo_files_lower = {p.name.lower() for p in repo.iterdir() if p.is_file()} if repo.is_dir() else set()

    for dep_dir, markers in LOCKFILE_BACKED_DEP_DIRS:
        target = repo / dep_dir
        # Case variants: Pods vs pods, Vendor vs vendor
        if not target.is_dir():
            matches = [p for p in repo.iterdir() if p.is_dir() and p.name.lower() == dep_dir]
            target = matches[0] if matches else target
        if not target.is_dir():
            continue
        if repo_files_lower & {m.lower() for m in markers}:
            roots.append(target.relative_to(repo).as_posix().rstrip("/") + "/")

    # Nested package roots (monorepos): package.json + node_modules, composer + vendor
    for root, dirs, files in os.walk(repo):
        dirs[:] = [
            d
            for d in dirs
            if d != ".git" and d.lower() not in {"node_modules", "vendor", "pods", ".venv", "venv"}
        ]
        files_lower = {name.lower() for name in files}
        rel = Path(root).relative_to(repo)
        for dep_dir, markers in LOCKFILE_BACKED_DEP_DIRS:
            if not (files_lower & {m.lower() for m in markers}):
                continue
            dep_path = Path(root) / dep_dir
            if not dep_path.is_dir():
                continue
            prefix = (
                (rel / dep_dir).as_posix().rstrip("/") + "/"
                if rel.as_posix() != "."
                else dep_dir.rstrip("/") + "/"
            )
            roots.append(prefix)

    carthage = repo / "Carthage" / "Checkouts"
    if carthage.is_dir() and (repo_files_lower & CARTHAGE_MARKERS):
        roots.append("Carthage/Checkouts/")
    # Deduplicate longest-first for prefix checks
    uniq = sorted({r.replace("\\", "/") for r in roots}, key=len, reverse=True)
    return uniq


def _collect_oss_license_roots(repo: Path) -> list[str]:
    """Directories whose LICENSE/COPYING text matches a recognized OSS license."""
    roots: list[str] = []
    skip_dir_names = {
        ".git",
        "node_modules",
        ".venv",
        "venv",
        "site-packages",
        "dist",
        "build",
        "__pycache__",
    }
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in skip_dir_names and not d.startswith(".")]
        for filename in files:
            if filename.lower() not in LICENSE_FILE_NAMES:
                continue
            path = Path(root) / filename
            if not _license_text_is_oss(_read_text_head(path)):
                continue
            rel = Path(root).relative_to(repo).as_posix()
            roots.append("" if rel == "." else rel.rstrip("/") + "/")
    uniq = sorted({r.replace("\\", "/") for r in roots}, key=len, reverse=True)
    return uniq


def _dir_under_roots(rel_dir: str, roots: list[str]) -> bool:
    """True if rel_dir ('' for repo root) is inside any root prefix.

    Root '' means a repo-root OSS LICENSE covers the entire tree.
    """
    if not roots:
        return False
    if "" in roots:
        return True
    if not rel_dir or rel_dir == ".":
        return False
    prefix = rel_dir.replace("\\", "/").rstrip("/") + "/"
    for root in roots:
        if not root:
            continue
        r = root.replace("\\", "/")
        if not r.endswith("/"):
            r += "/"
        if prefix.startswith(r) or prefix.rstrip("/") == r.rstrip("/"):
            return True
    return False


def path_bucket_loc_pcts(repo: Path) -> dict[str, float | str]:
    """LOC share for open-source (strong signals) vs library/framework/module paths.

    Open Source LOC uses strong evidence only:
      1) lockfile/manifest-backed dependency install trees (composer/npm/go/Pods/…)
      2) subtrees with a recognized OSS LICENSE/COPYING file
      3) files with SPDX-License-Identifier using a known OSS license id

    Library/modules LOC still uses path segments (node_modules, lib, modules, …).

    Denominator is all code-extension LOC under the checkout. Returns 0.0 when
    measured with no matches; blank only if no code LOC is present.
    """
    dep_roots = _collect_lockfile_backed_dep_roots(repo)
    license_roots = _collect_oss_license_roots(repo)

    total = 0
    open_source = 0
    library_modules = 0
    for root, dirs, files in os.walk(repo):
        dirs[:] = [directory for directory in dirs if directory != ".git"]
        rel_root = Path(root).relative_to(repo).as_posix().replace("\\", "/")
        if rel_root == ".":
            rel_root = ""
        parts = {p for p in rel_root.lower().split("/") if p}
        in_lib = bool(parts & LIBRARY_MODULE_DIR_NAMES)
        under_dep = _dir_under_roots(rel_root, dep_roots)
        under_license = _dir_under_roots(rel_root, license_roots)

        for filename in files:
            path = Path(root) / filename
            if path.suffix.lower() not in CODE_LOC_EXTENSIONS:
                continue
            lines = _count_file_lines(path)
            if lines <= 0:
                continue
            total += lines
            if in_lib:
                library_modules += lines
            # SPDX check only when not already covered (avoids scanning every vendored file).
            if under_dep or under_license or _file_has_oss_spdx(path):
                open_source += lines

    if total <= 0:
        return {"open_source_loc_pct": "", "library_modules_loc_pct": ""}
    return {
        "open_source_loc_pct": round(100.0 * open_source / total, 1),
        "library_modules_loc_pct": round(100.0 * library_modules / total, 1),
    }


def detect_library_code(repo: Path) -> bool:
    """True when the tree contains library-style directories or publishable package manifests."""
    for root, dirs, files in os.walk(repo):
        dirs[:] = [directory for directory in dirs if directory not in SKIP_WALK_DIRS]
        if Path(root).name.lower() in LIBRARY_DIR_NAMES:
            return True
        for filename in files:
            path = Path(root) / filename
            name = filename.lower()
            if name.endswith(".csproj"):
                sample = _read_text_sample(path, 20_000)
                lowered = sample.lower()
                if "<outputtype>library</outputtype>" in lowered:
                    return True
                if (
                    "microsoft.net.sdk" in lowered
                    and "<outputtype>exe</outputtype>" not in lowered
                    and "<outputtype>winexe</outputtype>" not in lowered
                ):
                    return True
            if name == "package.json":
                sample = _read_text_sample(path, 40_000)
                try:
                    data = json.loads(sample)
                except json.JSONDecodeError:
                    data = {}
                if isinstance(data, dict) and (
                    data.get("main") or data.get("exports") or data.get("module")
                ):
                    return True
            if name == "setup.py":
                sample = _read_text_sample(path, 40_000).lower()
                if "setup(" in sample and (
                    "find_packages" in sample or "packages=" in sample or "py_modules" in sample
                ):
                    return True
            if name == "pyproject.toml":
                sample = _read_text_sample(path, 40_000).lower()
                if "[project]" in sample or "[tool.poetry]" in sample or "packages =" in sample:
                    return True
            if name.endswith(".gemspec"):
                return True
    return False


def _commit_year(value: Any) -> int | None:
    text = str(value or "").strip()
    if len(text) < 4 or not text[:4].isdigit():
        return None
    year = int(text[:4])
    if 1970 <= year <= 2100:
        return year
    return None


def format_company_period(first_year: int | None, last_year: int | None) -> str:
    if first_year is None and last_year is None:
        return ""
    if first_year is None:
        return str(last_year)
    if last_year is None or first_year == last_year:
        return str(first_year)
    return f"{first_year}–{last_year}"


def apply_company_periods(rows: list[dict[str, Any]]) -> None:
    """Set company_period from earliest/latest commit years across repos in the same org."""
    bounds: dict[str, list[int | None]] = {}
    for row in rows:
        org = str(row.get("org") or "")
        first_year = _commit_year(row.get("first_commit"))
        last_year = _commit_year(row.get("last_commit_date"))
        current = bounds.setdefault(org, [None, None])
        if first_year is not None and (current[0] is None or first_year < current[0]):
            current[0] = first_year
        if last_year is not None and (current[1] is None or last_year > current[1]):
            current[1] = last_year
    for row in rows:
        org = str(row.get("org") or "")
        first_year, last_year = bounds.get(org, [None, None])
        row["company_period"] = format_company_period(first_year, last_year)


def local_org_and_full_name(root: Path, repo_path: Path) -> tuple[str, str]:
    """Derive org/group and full_name from local clone layout when possible."""
    relative = repo_path.relative_to(root)
    parts = relative.parts
    if not parts or parts == (".",):
        return root.name, root.name
    if len(parts) >= 3 and parts[0] in LOCAL_PLATFORM_ROOTS:
        org = parts[1]
        return org, "/".join(parts[1:])
    if len(parts) >= 2:
        return parts[0], str(relative).replace("\\", "/")
    return root.name, parts[0]


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
            org, full_name = local_org_and_full_name(root, current_path)
            repos.append(
                RepoTarget(
                    platform="local",
                    org=org,
                    full_name=full_name,
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
            org, full_name = local_org_and_full_name(root, current_path)
            repos.append(
                RepoTarget(
                    platform="local",
                    org=org,
                    full_name=full_name,
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


def list_github_accessible_repo_objects(
    token: str,
    host: str = "github.com",
    *,
    affiliation: str = "owner,collaborator,organization_member",
) -> list[dict[str, Any]]:
    """Repos the token can access, including direct collaborator grants."""
    api = github_api(token, host)
    encoded = urllib.parse.quote(affiliation, safe=",")
    url = (
        f"{api}/user/repos?per_page=100&affiliation={encoded}"
        "&visibility=all&sort=full_name"
    )
    repos = paginate_github(url, token)
    if not repos:
        raise RuntimeError(
            "No GitHub repositories found for this token "
            "(owner, collaborator, or organization member)."
        )
    return [repo for repo in repos if isinstance(repo, dict) and repo.get("full_name")]


def list_github_accessible_repos(
    token: str, host: str = "github.com"
) -> list[dict[str, str]]:
    return [
        {
            "id": str(repo["full_name"]),
            "name": str(repo["full_name"]),
            "archived": bool(repo.get("archived")),
        }
        for repo in list_github_accessible_repo_objects(token, host)
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


def list_gitlab_accessible_projects(
    token: str, host: str = "gitlab.com"
) -> list[dict[str, str]]:
    return [
        {
            "id": str(project["path_with_namespace"]),
            "name": str(project["path_with_namespace"]),
            "archived": bool(project.get("archived")),
        }
        for project in list_gitlab_accessible_project_objects(token, host)
    ]


def list_bitbucket_repos_for_workspace(
    token: str, workspace: str, email: str = ""
) -> list[dict[str, str]]:
    return [
        {"id": str(repo["full_name"]), "name": str(repo["full_name"]), "archived": False}
        for repo in list_bitbucket_repo_objects(token, workspace, email)
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
            "project_name": org,
            "repo": repo,
            "merged_prs": 0,
            "loc": 0,
            "total_files": 0,
            "has_tests": False,
            "has_test_runner": False,
            "has_ci_cd": False,
            "has_library_code": False,
            "open_source_loc_pct": "",
            "library_modules_loc_pct": "",
            "error": "",
            "error_class": "",
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
    bitbucket_token_name: str = DEFAULT_BITBUCKET_TOKEN_NAME,
    bitbucket_email_name: str = DEFAULT_BITBUCKET_EMAIL_NAME,
    llm_config: LLMConfig | None,
    run_dir: Path,
    clones_dir: Path,
    github_host: str,
    gitlab_host: str,
    log: logging.Logger,
    clone_timeout: int = DEFAULT_CLONE_TIMEOUT_SECONDS,
    clone_retries: int = DEFAULT_CLONE_RETRIES,
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
    # PR-tier percentages exist only when the platform API supplied PR detail;
    # git-history and local runs leave them blank rather than guessing.
    pr_tiers: tuple[dict[str, Any], list[dict[str, Any]]] | None = None

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
            pr_tiers = pr_tier_metrics("github", prs)

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
            pr_tiers = pr_tier_metrics("gitlab", mrs)

            clone_url = gitlab_clone_url(target.full_name, token, gitlab_host)
        elif target.platform == "bitbucket":
            token = tokens[bitbucket_token_name]
            email = tokens.get(bitbucket_email_name, "")
            row["repo_created_at"] = meta.get("created_on") or ""
            row["primary_language"] = meta.get("language") or ""
            if meta.get("size") is not None:
                row["size_kb"] = int(meta["size"] / 1024)

            # Bitbucket exposes neither a language breakdown nor a contributor
            # list; both are derived from the clone (SCC / git authors) below.
            prs = fetch_bitbucket_merged_prs(token, target.full_name, email)
            write_json(api_dir / "merged_prs.json", prs)
            row["merged_prs"] = len(prs)
            pr_tiers = pr_tier_metrics("bitbucket", prs)

            clone_url = bitbucket_clone_url(target.full_name, token)
        else:
            if target.local_path is None:
                raise RuntimeError("Local repository path is missing")
            probe_local_git(clone_path, log)
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
            clone_repo(
                clone_url,
                clone_path,
                timeout=clone_timeout,
                retries=clone_retries,
                log=log,
            )

        if target.platform != "local":
            row["measured_branch"] = (
                row["default_branch"]
                if USE_DEFAULT_BRANCH
                else select_latest_branch(clone_path, row["default_branch"], log)
                or row["default_branch"]
            )
        else:
            row["measured_branch"] = row["default_branch"]

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

        export_commits_jsonl(
            clone_path,
            git_dir / "commits.jsonl",
            total_commits=git_stats.get("total_commits"),
            log=log,
        )

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

        if target.platform == "bitbucket":
            row["contributor_count"] = (
                git_stats["human_authors"] + git_stats["bot_authors"]
            )

        if target.platform != "local" and not row["merged_prs"]:
            # The platform API reported no merged PRs/MRs; fall back to merge
            # and squash markers recovered from git history.
            detected_prs = detect_merged_prs_from_git(clone_path)
            write_json(git_dir / "merged_prs_detected_from_git.json", detected_prs)
            row["merged_prs"] = len(detected_prs)

        row["project_name"] = org
        row["total_files"] = count_total_files(clone_path)
        row["has_tests"] = detect_tests(clone_path)
        row["has_test_runner"] = detect_test_runner(clone_path)
        row["has_ci_cd"] = detect_ci_cd(clone_path)
        row["test_source_loc_pct"] = test_source_loc_pct(clone_path)
        row["has_library_code"] = detect_library_code(clone_path)
        path_pcts = path_bucket_loc_pcts(clone_path)
        row["open_source_loc_pct"] = path_pcts["open_source_loc_pct"]
        row["library_modules_loc_pct"] = path_pcts["library_modules_loc_pct"]
        write_json(git_dir / "loc_path_pcts.json", path_pcts)
        row.update(
            structure_metrics(
                clone_path,
                is_test=_path_is_test,
                skip_dirs=SKIP_WALK_DIRS,
            )
        )
        if pr_tiers is not None:
            row.update(pr_tiers[0])
            write_json(api_dir / "pr_tiers.json", pr_tiers[1])
        if llm_config is not None:
            try:
                row.update(run_llm_analysis(clone_path, row, llm_config))
            except Exception as exc:
                row["llm_analysis_error"] = str(exc)[:500]

        if target.platform != "local":
            try:
                shutil.rmtree(clone_path)
            except OSError as exc:
                log.warning(
                    "Failed to delete clone %s: %s (disk space may accumulate)",
                    target.full_name,
                    exc,
                )
            except Exception as exc:
                log.warning(
                    "Unexpected error deleting clone %s: %s",
                    target.full_name,
                    exc,
                )
        log.info(
            "OK %s: merged_prs=%s loc=%s span_days=%s",
            target.full_name,
            row["merged_prs"],
            row["loc"],
            row["span_days"],
        )
    except Exception as exc:
        row["error"] = str(exc)[:500]
        row["error_class"] = classify_error(exc)
        log.exception(
            "FAIL %s [%s]: %s",
            target.full_name,
            row["error_class"],
            row["error"],
        )
        if target.platform != "local":
            try:
                shutil.rmtree(clone_path)
            except OSError as exc:
                # If the original error was disk_full, escalate cleanup failure to CRITICAL
                if row["error_class"] == "disk_full":
                    log.critical(
                        "CRITICAL: Cannot delete clone directory %s. Disk space may accumulate. "
                        "Manual cleanup required: rm -rf %s\nCleanup error: %s",
                        target.full_name,
                        clone_path,
                        exc,
                    )
                else:
                    log.warning(
                        "Failed to delete clone %s: %s",
                        target.full_name,
                        exc,
                    )
            except Exception as exc:
                log.warning(
                    "Unexpected error deleting clone %s: %s",
                    target.full_name,
                    exc,
                )

    return row


def zip_run_dir(run_dir: Path) -> Path:
    zip_path = run_dir.with_suffix(".zip")
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in run_dir.rglob("*"):
            if path.is_file():
                # Keep the live clone cache out of the deliverable archive.
                if "clones/" in path.relative_to(run_dir).as_posix():
                    continue
                zf.write(path, path.relative_to(run_dir.parent).as_posix())
    return zip_path


def serialize_targets(targets: list[RepoTarget]) -> list[dict[str, Any]]:
    return [
        {
            "platform": target.platform,
            "org": target.org,
            "full_name": target.full_name,
            "meta": target.meta,
            "local_path": str(target.local_path) if target.local_path else None,
        }
        for target in targets
    ]


def load_targets_from_repos_json(path: Path) -> list[RepoTarget] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, list) or not data:
        return None
    targets: list[RepoTarget] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        full_name = str(item.get("full_name") or "").strip()
        org = str(item.get("org") or "").strip()
        platform = str(item.get("platform") or "").strip()
        if not full_name or not org or not platform:
            continue
        local_raw = item.get("local_path")
        local_path = Path(local_raw) if local_raw else None
        meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
        targets.append(
            RepoTarget(
                platform=platform,
                org=org,
                full_name=full_name,
                meta=meta,
                local_path=local_path,
            )
        )
    return targets or None


def filter_targets_for_resume(
    targets: list[RepoTarget],
    checkpoint: JobCheckpoint,
    *,
    retry_failed: bool,
    retry_all_failed: bool,
) -> tuple[list[RepoTarget], int, int]:
    """Return (to_process, skipped_ok, skipped_failed)."""
    ok_keys = checkpoint.completed_ok_keys()
    failed_keys = checkpoint.failed_keys(retryable_only=not retry_all_failed)
    all_failed_keys = checkpoint.failed_keys(retryable_only=False)
    to_process: list[RepoTarget] = []
    skipped_ok = 0
    skipped_failed = 0
    for target in targets:
        key = target.full_name.strip("/")
        if key in ok_keys:
            skipped_ok += 1
            continue
        if key in all_failed_keys:
            if retry_failed and key in failed_keys:
                to_process.append(target)
            else:
                skipped_failed += 1
            continue
        to_process.append(target)
    return to_process, skipped_ok, skipped_failed


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

    if getattr(args, "github_accessible", False):
        gh_token = resolve_github_token(tokens, github_token_name, github_token_fn)
        for meta in list_github_accessible_repo_objects(gh_token, args.github_host):
            full_name = str(meta["full_name"])
            targets.append(
                RepoTarget(
                    platform="github",
                    org=full_name.split("/", 1)[0],
                    full_name=full_name,
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
        group_targets: list[RepoTarget] = []
        for meta in list_gitlab_project_objects(
            tokens[gitlab_token_name], group, args.gitlab_host
        ):
            group_targets.append(
                RepoTarget(
                    platform="gitlab",
                    org=group,
                    full_name=meta["path_with_namespace"],
                    meta=meta,
                )
            )
        if args.gitlab_repo:
            group_targets = filter_local_targets(group_targets, args.gitlab_repo)
        targets.extend(group_targets)

    if getattr(args, "gitlab_accessible", False):
        if gitlab_token_name not in tokens:
            raise SystemExit(f"Missing {gitlab_token_name!r} in tokens file")
        for meta in list_gitlab_accessible_project_objects(
            tokens[gitlab_token_name], args.gitlab_host
        ):
            full_name = str(meta["path_with_namespace"])
            targets.append(
                RepoTarget(
                    platform="gitlab",
                    org=full_name.split("/", 1)[0],
                    full_name=full_name,
                    meta=meta,
                )
            )

    if args.gitlab_repo and not args.gitlab_group:
        if gitlab_token_name not in tokens:
            raise SystemExit(f"Missing {gitlab_token_name!r} in tokens file")
        for path_with_namespace in args.gitlab_repo:
            meta = fetch_gitlab_project(
                tokens[gitlab_token_name],
                path_with_namespace.strip("/"),
                args.gitlab_host,
            )
            full_name = str(meta["path_with_namespace"])
            targets.append(
                RepoTarget(
                    platform="gitlab",
                    org=full_name.split("/", 1)[0],
                    full_name=full_name,
                    meta=meta,
                )
            )

    bb_workspaces = getattr(args, "bitbucket_workspace", None) or []
    bb_repos = getattr(args, "bitbucket_repo", None) or []
    if bb_workspaces or bb_repos:
        bb_token_name = getattr(args, "bitbucket_token_name", DEFAULT_BITBUCKET_TOKEN_NAME)
        if bb_token_name not in tokens:
            raise SystemExit(f"Missing {bb_token_name!r} in tokens file")
        bb_token = tokens[bb_token_name]
        bb_email = tokens.get(
            getattr(args, "bitbucket_email_name", DEFAULT_BITBUCKET_EMAIL_NAME), ""
        )
        for workspace in bb_workspaces:
            ws_targets = [
                RepoTarget(
                    platform="bitbucket",
                    org=workspace,
                    full_name=str(meta["full_name"]),
                    meta=meta,
                )
                for meta in list_bitbucket_repo_objects(bb_token, workspace, bb_email)
            ]
            if bb_repos:
                ws_targets = filter_local_targets(ws_targets, bb_repos)
            targets.extend(ws_targets)
        if bb_repos and not bb_workspaces:
            for full_name in bb_repos:
                meta = fetch_bitbucket_repo(bb_token, full_name.strip("/"), bb_email)
                full = str(meta["full_name"])
                targets.append(
                    RepoTarget(
                        platform="bitbucket",
                        org=full.split("/", 1)[0],
                        full_name=full,
                        meta=meta,
                    )
                )

    if not targets:
        raise SystemExit(
            "Provide --github-org / --github-repo / --github-accessible / "
            "--gitlab-group / --gitlab-repo / --gitlab-accessible / "
            "--bitbucket-workspace / --bitbucket-repo"
        )
    # De-duplicate by platform + full_name while preserving order.
    seen: set[tuple[str, str]] = set()
    unique: list[RepoTarget] = []
    for target in targets:
        key = (target.platform, target.full_name)
        if key in seen:
            continue
        seen.add(key)
        unique.append(target)
    return unique


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
    parser.add_argument(
        "--clone-timeout",
        type=int,
        default=DEFAULT_CLONE_TIMEOUT_SECONDS,
        help=(
            "Seconds before a git clone is aborted (default: "
            f"{DEFAULT_CLONE_TIMEOUT_SECONDS}). Failed clones are skipped so the run continues."
        ),
    )
    parser.add_argument(
        "--clone-retries",
        type=int,
        default=DEFAULT_CLONE_RETRIES,
        help=(
            "Extra clone attempts after a timeout/network failure "
            f"(default: {DEFAULT_CLONE_RETRIES})."
        ),
    )
    parser.add_argument(
        "--resume",
        type=Path,
        default=None,
        help="Resume an interrupted run directory (skips repos already marked OK in summary.csv)",
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="With --resume, re-process failed repos (retryable errors by default; see --retry-all-failed)",
    )
    parser.add_argument(
        "--retry-all-failed",
        action="store_true",
        help="With --resume --retry-failed, retry every failed repo, not only timeout/network/rate-limit",
    )
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
        "--bitbucket-token-name",
        default=DEFAULT_BITBUCKET_TOKEN_NAME,
        help=f"Key in tokens file for Bitbucket (default: {DEFAULT_BITBUCKET_TOKEN_NAME})",
    )
    parser.add_argument(
        "--bitbucket-email-name",
        default=DEFAULT_BITBUCKET_EMAIL_NAME,
        help=(
            "Key in tokens file holding the Atlassian email used for REST Basic auth "
            f"(default: {DEFAULT_BITBUCKET_EMAIL_NAME}); pass an empty string to send "
            "the token as a Bearer (workspace/repository access tokens)"
        ),
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
    parser.add_argument(
        "--github-accessible",
        action="store_true",
        help=(
            "Include every GitHub repository the token can access "
            "(owner, collaborator, and organization member affiliations)"
        ),
    )
    parser.add_argument("--gitlab-group", action="append", default=[])
    parser.add_argument(
        "--gitlab-repo",
        action="append",
        default=[],
        help=(
            "GitLab project path (group/project). Alone: analyse these projects. "
            "With --gitlab-group: include only matching names or paths (repeatable)"
        ),
    )
    parser.add_argument(
        "--gitlab-accessible",
        action="store_true",
        help=(
            "Include every GitLab project the token can access via membership "
            "(any group or personal namespace)"
        ),
    )
    parser.add_argument(
        "--bitbucket-workspace",
        action="append",
        default=[],
        help="Bitbucket Cloud workspace slug (repeatable). There is no account-wide discovery",
    )
    parser.add_argument(
        "--bitbucket-repo",
        action="append",
        default=[],
        help=(
            "Bitbucket repository (workspace/repo). Alone: analyse these repositories. "
            "With --bitbucket-workspace: include only matching names or paths"
        ),
    )
    args = parser.parse_args()

    if args.ui:
        from extract_ui import serve

        serve(host=args.ui_host, port=args.ui_port)
        return 0

    if args.retry_failed and not args.resume:
        raise SystemExit("--retry-failed requires --resume")
    if args.clone_timeout < 30:
        raise SystemExit("--clone-timeout must be at least 30 seconds")
    if args.clone_retries < 0:
        raise SystemExit("--clone-retries must be >= 0")

    resume_dir: Path | None = None
    resumed_targets: list[RepoTarget] | None = None
    if args.resume:
        resume_dir = args.resume.expanduser().resolve()
        if not resume_dir.is_dir():
            raise SystemExit(f"--resume directory not found: {resume_dir}")
        resumed_targets = load_targets_from_repos_json(resume_dir / "repos.json")
        if resumed_targets and all(t.platform == "local" for t in resumed_targets):
            args.offline = True

    if args.offline and not args.local_repos_dir and resumed_targets is None:
        raise SystemExit("--offline requires --local-repos-dir")
    if args.offline and (
        args.github_org
        or args.github_repo
        or args.github_accessible
        or args.gitlab_group
        or args.gitlab_repo
        or args.gitlab_accessible
        or args.bitbucket_workspace
        or args.bitbucket_repo
    ):
        raise SystemExit(
            "--offline cannot be combined with GitHub, GitLab or Bitbucket targets"
        )
    if args.llm and not os.environ.get("OPENAI_API_KEY", "").strip():
        raise SystemExit(
            "--llm requires OPENAI_API_KEY in the environment. "
            "Do not pass API keys as command-line arguments."
        )

    tokens: dict[str, str] = {}
    # A token pasted into the local UI arrives through the environment (never on
    # the command line) so it stays out of argv, the logs, and the output zip —
    # the same channel already used for OPENAI_API_KEY.
    ui_github_token = os.environ.get("EXTRACT_GITHUB_TOKEN", "").strip()
    ui_gitlab_token = os.environ.get("EXTRACT_GITLAB_TOKEN", "").strip()
    ui_bitbucket_token = os.environ.get("EXTRACT_BITBUCKET_TOKEN", "").strip()
    ui_bitbucket_email = os.environ.get("EXTRACT_BITBUCKET_EMAIL", "").strip()
    # Offline mode analyses local clones only and never uses API tokens.
    # Skip loading so a Docker bind-mount directory at the default path
    # (created when the host tokens file was missing) cannot abort the run.
    if not args.offline:
        if args.tokens_file.is_file():
            try:
                tokens = parse_tokens_file(args.tokens_file)
            except ValueError as exc:
                raise SystemExit(str(exc)) from exc
        elif args.tokens_file.is_dir():
            raise SystemExit(
                f"Tokens path is a directory, expected a file: {args.tokens_file}. "
                "If Docker created this after a missing bind mount, remove the "
                "directory on the host and copy tokens.example to tokens."
            )
        elif (
            not args.list_installations
            and not args.github_app
            and not (ui_github_token or ui_gitlab_token or ui_bitbucket_token)
        ):
            raise SystemExit(f"Tokens file not found: {args.tokens_file}")
        # A UI-pasted token overrides / creates the matching key so the rest of
        # the pipeline (resolve_github_token, build_targets, process_repo) works
        # unchanged whether the value came from the file or the UI field.
        if ui_github_token:
            tokens[args.github_token_name] = ui_github_token
        if ui_gitlab_token:
            tokens[args.gitlab_token_name] = ui_gitlab_token
        if ui_bitbucket_token:
            tokens[args.bitbucket_token_name] = ui_bitbucket_token
            # A pasted token carries its own email (blank means Bearer); never
            # pair it with an email left over in the tokens file.
            if args.bitbucket_email_name:
                tokens[args.bitbucket_email_name] = ui_bitbucket_email

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

    all_targets: list[RepoTarget] | None = resumed_targets

    if all_targets is None:
        if args.offline:
            all_targets = find_local_repositories(args.local_repos_dir.resolve())
            if args.local_repo:
                all_targets = filter_local_targets(all_targets, args.local_repo)
        else:
            all_targets = build_targets(
                args,
                tokens,
                github_token_name,
                args.gitlab_token_name,
                github_token_fn=github_token_fn,
            )

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if resume_dir is not None:
        run_dir = resume_dir
        # Prefer the original stamp from the folder name when present.
        stamp = run_dir.name.rsplit("-", 1)[-1] if "-" in run_dir.name else stamp
    else:
        if args.offline:
            label = args.local_repos_dir.name
        elif args.github_org:
            label = args.github_org[0]
        elif args.gitlab_group:
            label = args.gitlab_group[0]
        elif args.bitbucket_workspace:
            label = args.bitbucket_workspace[0]
        elif args.github_accessible:
            label = "github-accessible"
        elif args.gitlab_accessible:
            label = "gitlab-accessible"
        elif args.github_repo:
            label = args.github_repo[0].replace("/", "_")
        elif args.gitlab_repo:
            label = args.gitlab_repo[0].replace("/", "_")
        elif args.bitbucket_repo:
            label = args.bitbucket_repo[0].replace("/", "_")
        else:
            label = "repos"
        run_dir = args.output_dir / f"raw-extract-{safe_name(label)}-{stamp}"

    clones_dir = run_dir / "clones"
    logs_dir = run_dir / "logs"
    run_dir.mkdir(parents=True, exist_ok=True)
    clones_dir.mkdir(parents=True, exist_ok=True)

    log = setup_logger(logs_dir / "extract.log")
    # Emit early so the UI can offer partial downloads while the run is active.
    print(f"JOB_DIR={run_dir}", flush=True)
    log.info("JOB_DIR=%s", run_dir)

    checkpoint = JobCheckpoint.load_or_create(run_dir, total=len(all_targets))
    targets, skipped_ok, skipped_failed = filter_targets_for_resume(
        all_targets,
        checkpoint,
        retry_failed=bool(args.retry_failed),
        retry_all_failed=bool(args.retry_all_failed),
    )
    if resume_dir is not None:
        log.info(
            "Resume mode: %s pending, skipped_ok=%s skipped_failed=%s retry_failed=%s",
            len(targets),
            skipped_ok,
            skipped_failed,
            bool(args.retry_failed),
        )

    log.info("Extracting %s repos → %s", len(all_targets), run_dir)
    log.info(
        "This pass will process %s repos (clone_timeout=%ss clone_retries=%s workers=%s)",
        len(targets),
        args.clone_timeout,
        args.clone_retries,
        args.workers,
    )
    log_runtime_diagnostics(log, args)
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
    log.info(
        "Support: if this run fails, send %s and %s",
        logs_dir / "extract.log",
        logs_dir / "failures.json",
    )

    write_json(run_dir / "repos.json", serialize_targets(all_targets))
    checkpoint.total = len(all_targets)
    checkpoint.persist()
    checkpoint.write_summary()

    if not targets:
        log.info("Nothing left to process; refreshing final artifacts from checkpoint")
    else:
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            futures = {
                pool.submit(
                    process_repo,
                    t,
                    tokens=tokens,
                    github_token_name=github_token_name,
                    gitlab_token_name=args.gitlab_token_name,
                    bitbucket_token_name=args.bitbucket_token_name,
                    bitbucket_email_name=args.bitbucket_email_name,
                    github_token_fn=github_token_fn,
                    llm_config=llm_config,
                    run_dir=run_dir,
                    clones_dir=clones_dir,
                    github_host=args.github_host,
                    gitlab_host=args.gitlab_host,
                    log=log,
                    clone_timeout=args.clone_timeout,
                    clone_retries=args.clone_retries,
                ): t
                for t in targets
            }
            for fut in as_completed(futures):
                target = futures[fut]
                try:
                    row = fut.result()
                except Exception as exc:
                    log.exception("Worker crashed while collecting a repository result")
                    row = empty_summary_row(target.org, target.full_name.split("/")[-1])
                    row["error"] = f"worker crash: {exc}"[:500]
                    row["error_class"] = classify_error(exc)
                checkpoint.record(target.full_name, row)
                progress = checkpoint.progress_snapshot()
                log.info(
                    "Progress %s/%s ok=%s failed=%s",
                    progress["done"],
                    progress["total"],
                    progress["ok"],
                    progress["failed"],
                )

    rows = checkpoint.rows()
    apply_company_periods(rows)
    checkpoint.replace_rows(rows)

    summary_path = checkpoint.summary_path
    failures = [
        {
            "org": row.get("org"),
            "repo": row.get("repo"),
            "error": row.get("error"),
            "error_class": row.get("error_class"),
        }
        for row in rows
        if row.get("error")
    ]
    write_json(logs_dir / "failures.json", failures)
    if failures:
        log.error("Repository failures: %s of %s", len(failures), len(rows))
        by_class: dict[str, int] = {}
        for failure in failures:
            error_class = str(failure.get("error_class") or "unknown")
            by_class[error_class] = by_class.get(error_class, 0) + 1
        log.error("Failure classes: %s", by_class)
        for failure in failures[:50]:
            log.error(
                "Failed repo %s/%s [%s]: %s",
                failure.get("org"),
                failure.get("repo"),
                failure.get("error_class") or "unknown",
                failure.get("error"),
            )
        if len(failures) > 50:
            log.error("…and %s more failures (see failures.json)", len(failures) - 50)

    support_note = (
        "If you need help debugging this run, send these files to support:\n"
        f"- {logs_dir / 'extract.log'}\n"
        f"- {logs_dir / 'failures.json'}\n"
        f"- {run_dir / 'job.json'}\n"
        f"- {run_dir / 'manifest.json'}\n"
        "Do not send your tokens file or API keys.\n"
        "\n"
        "To continue an interrupted or partial run:\n"
        f"  python extract_org_raw_data.py --resume {run_dir}\n"
        "To retry timeout/network/rate-limit failures:\n"
        f"  python extract_org_raw_data.py --resume {run_dir} --retry-failed\n"
    )
    (logs_dir / "SUPPORT.txt").write_text(support_note, encoding="utf-8")
    log.info("Wrote support note: %s", logs_dir / "SUPPORT.txt")

    shutil.rmtree(clones_dir, ignore_errors=True)

    progress = checkpoint.progress_snapshot()
    manifest = {
        "created_at": stamp,
        "repos": len(all_targets),
        "ok": progress["ok"],
        "failed": progress["failed"],
        "error_classes": progress.get("error_classes") or {},
        "summary_csv": str(summary_path),
        "job_json": str(checkpoint.job_path),
        "resumable": True,
        "clone_timeout_seconds": args.clone_timeout,
        "clone_retries": args.clone_retries,
        "support_logs": {
            "extract_log": str(logs_dir / "extract.log"),
            "failures_json": str(logs_dir / "failures.json"),
            "support_txt": str(logs_dir / "SUPPORT.txt"),
        },
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
            "project_name": "Same as org/group name.",
            "total_files": (
                "File count excluding .git and common dependency/build directories."
            ),
            "has_ci_cd": "Presence of common CI/CD config files or workflow directories.",
            "has_test_runner": (
                "Presence of test-runner config files or test SDK references."
            ),
            "test_source_loc_pct": (
                "Static test:source line ratio percentage; not executed coverage."
            ),
            "has_library_code": (
                "Library folders or publishable package/class-library manifests."
            ),
            "open_source_loc_pct": (
                "Share of code LOC with strong OSS evidence: lockfile-backed "
                "dependency trees, recognized OSS LICENSE/COPYING text, or "
                "SPDX-License-Identifier with a known OSS license."
            ),
            "library_modules_loc_pct": (
                "Share of code LOC under library/framework/module path segments "
                "(node_modules, lib, modules, frameworks, …)."
            ),
            "function_count": "Named functions/methods found by tree-sitter in production source files.",
            "class_count": "Classes, interfaces, structs, enums and traits found by tree-sitter in production source files.",
            "docstring_coverage_pct": "Share of functions with a docstring (Python) or an adjacent doc comment (other languages).",
            "comment_docstring_ratio": "SCC comment lines / (comment + code lines) over production source files only (same file set as function_count); SCC counts docstrings as comments.",
            "untested_files": "Production source files not imported by any test (import resolution for Python, JS/TS, Java/Kotlin/Scala) and without a same-named test in a mirrored directory (static heuristic, not coverage).",
            "untested_files_pct": "untested_files as a share of production source files.",
            "company_period": (
                "Earliest first_commit year through latest last_commit year "
                "across all repos in the same org/group."
            ),
        }
    write_json(run_dir / "manifest.json", manifest)

    zip_path = zip_run_dir(run_dir)
    log.info("Done. summary=%s", summary_path)
    log.info("Zip=%s", zip_path)
    if failures:
        log.info(
            "Completed with errors. Resume pending repos with: "
            "python extract_org_raw_data.py --resume %s",
            run_dir,
        )
        log.info(
            "Retry timeout/network/rate-limit failures with: "
            "python extract_org_raw_data.py --resume %s --retry-failed",
            run_dir,
        )
    print(
        json.dumps(
            {"run_dir": str(run_dir), "zip": str(zip_path), **manifest},
            indent=2,
        )
    )
    return 0 if manifest["failed"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
