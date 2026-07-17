#!/usr/bin/env python3
"""Clone all GitHub/GitLab repos locally, then offline analysis can run without API calls."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from extract_org_raw_data import (
    clone_repo,
    github_clone_url,
    gitlab_clone_url,
    list_github_orgs_for_token,
    list_github_repo_objects,
    list_gitlab_groups_for_token,
    list_gitlab_project_objects,
    parse_tokens_file,
    safe_name,
)

CODING = Path(__file__).resolve().parent
DEFAULT_REPOS_ROOT = CODING / "repos"


def log(msg: str) -> None:
    print(msg, flush=True)


def is_cloned(dest: Path) -> bool:
    return (dest / ".git").is_dir()


def list_repos_with_retry(fn, *args, retries: int = 6, **kwargs):
    delay = 30
    for attempt in range(1, retries + 1):
        try:
            return fn(*args, **kwargs)
        except RuntimeError as exc:
            if "RATE_LIMIT" not in str(exc).upper() and "rate limit" not in str(exc).lower():
                raise
            if attempt == retries:
                raise
            log(f"  rate limited; retry {attempt}/{retries - 1} in {delay}s")
            time.sleep(delay)
            delay = min(delay * 2, 900)


def clone_github_org(
    token: str,
    org: str,
    dest_root: Path,
    *,
    host: str = "github.com",
    skip_existing: bool = True,
) -> dict[str, int]:
    org_dir = dest_root / "github" / safe_name(org)
    org_dir.mkdir(parents=True, exist_ok=True)
    stats = {"listed": 0, "cloned": 0, "skipped": 0, "failed": 0}

    try:
        repos = list_repos_with_retry(list_github_repo_objects, token, org, host)
    except RuntimeError as exc:
        if "No GitHub repos found" in str(exc):
            log(f"[github] {org}: 0 repos (skipping)")
            return stats
        raise
    stats["listed"] = len(repos)
    log(f"[github] {org}: {len(repos)} repos")

    for meta in repos:
        full_name = str(meta["full_name"])
        repo_name = full_name.split("/", 1)[-1]
        dest = org_dir / safe_name(repo_name)
        if skip_existing and is_cloned(dest):
            stats["skipped"] += 1
            continue
        url = github_clone_url(full_name, token, host)
        try:
            log(f"  cloning {full_name}")
            clone_repo(url, dest)
            stats["cloned"] += 1
        except Exception as exc:
            stats["failed"] += 1
            log(f"  FAIL {full_name}: {exc}")
        time.sleep(2)
    return stats


def clone_gitlab_group(
    token: str,
    group: str,
    dest_root: Path,
    *,
    host: str = "gitlab.com",
    skip_existing: bool = True,
) -> dict[str, int]:
    group_dir = dest_root / "gitlab" / safe_name(group.replace("/", "__"))
    group_dir.mkdir(parents=True, exist_ok=True)
    stats = {"listed": 0, "cloned": 0, "skipped": 0, "failed": 0}

    try:
        projects = list_repos_with_retry(list_gitlab_project_objects, token, group, host)
    except RuntimeError as exc:
        if "No GitLab projects found" in str(exc):
            log(f"[gitlab] {group}: 0 projects (skipping)")
            return stats
        raise
    stats["listed"] = len(projects)
    log(f"[gitlab] {group}: {len(projects)} projects")

    for meta in projects:
        full_name = str(meta["path_with_namespace"])
        relative = full_name
        if full_name.startswith(group + "/"):
            relative = full_name[len(group) + 1 :]
        elif full_name == group:
            relative = str(meta.get("path") or full_name.split("/")[-1])
        dest = group_dir / Path(relative)
        if skip_existing and is_cloned(dest):
            stats["skipped"] += 1
            continue
        url = gitlab_clone_url(full_name, token, host)
        try:
            log(f"  cloning {full_name}")
            clone_repo(url, dest)
            stats["cloned"] += 1
        except Exception as exc:
            stats["failed"] += 1
            log(f"  FAIL {full_name}: {exc}")
        time.sleep(2)
    return stats


def top_level_gitlab_groups(groups: list[dict[str, str]]) -> list[str]:
    ids = [g["id"] for g in groups]
    top = []
    for group_id in ids:
        if any(
            other != group_id and group_id.startswith(other + "/") for other in ids
        ):
            continue
        top.append(group_id)
    return sorted(top)


def main() -> int:
    parser = argparse.ArgumentParser(description="Clone all org/group repos locally")
    parser.add_argument("--tokens-file", type=Path, default=CODING / "tokens")
    parser.add_argument("--repos-root", type=Path, default=DEFAULT_REPOS_ROOT)
    parser.add_argument("--github-token-name", default="data-lh2-token-github")
    parser.add_argument("--gitlab-token-name", default="data-lh2-token-gitlab")
    parser.add_argument("--github-host", default="github.com")
    parser.add_argument("--gitlab-host", default="gitlab.com")
    parser.add_argument("--targets-file", type=Path, default=CODING / "discovered_targets.json")
    parser.add_argument("--no-skip-existing", action="store_true")
    args = parser.parse_args()

    tokens = parse_tokens_file(args.tokens_file)
    gh_token = tokens.get(args.github_token_name)
    gl_token = tokens.get(args.gitlab_token_name)
    if not gh_token:
        raise SystemExit(f"Missing {args.github_token_name!r} in tokens file")
    if not gl_token:
        raise SystemExit(f"Missing {args.gitlab_token_name!r} in tokens file")

    args.repos_root.mkdir(parents=True, exist_ok=True)
    skip_existing = not args.no_skip_existing
    summary: dict[str, dict[str, int]] = {}

    if args.targets_file.exists():
        targets = json.loads(args.targets_file.read_text(encoding="utf-8"))
        github_orgs = list(targets.get("github_orgs") or [])
        gitlab_groups = list(targets.get("gitlab_groups") or [])
        log(f"Loaded targets file: {len(github_orgs)} GitHub, {len(gitlab_groups)} GitLab")
    else:
        github_orgs = [o["id"] for o in list_github_orgs_for_token(gh_token, args.github_host)]
        gitlab_groups = top_level_gitlab_groups(
            list_gitlab_groups_for_token(gl_token, args.gitlab_host)
        )

    log(f"GitHub orgs/users: {len(github_orgs)}")
    for org in github_orgs:
        summary[f"github:{org}"] = clone_github_org(
            gh_token,
            org,
            args.repos_root,
            host=args.github_host,
            skip_existing=skip_existing,
        )

    log(f"GitLab top-level groups: {len(gitlab_groups)}")
    for group in gitlab_groups:
        summary[f"gitlab:{group}"] = clone_gitlab_group(
            gl_token,
            group,
            args.repos_root,
            host=args.gitlab_host,
            skip_existing=skip_existing,
        )

    log(json.dumps({"summary": summary}, indent=2))
    failed = sum(v["failed"] for v in summary.values())
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
