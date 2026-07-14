#!/usr/bin/env python3
"""
Count merged pull/merge requests across GitHub and GitLab.

Examples:
  export GITHUB_TOKEN=ghp_...
  export GITLAB_TOKEN=glpat-...

  python count_merged_prs.py --github-org my-org --gitlab-group my-group
  python count_merged_prs.py --github-repo owner/repo --gitlab-project group/project
  python count_merged_prs.py --github-org my-org --since 2025-01-01 --json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any


def http_get_json(url: str, headers: dict[str, str]) -> tuple[Any, dict[str, str]]:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = resp.read().decode("utf-8")
            hdrs = {k: v for k, v in resp.headers.items()}
            return (json.loads(body) if body else None), hdrs
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} for {url}: {detail}") from exc


def paginate_github(url: str, token: str) -> list[Any]:
    items: list[Any] = []
    page = 1
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "count-merged-prs",
    }
    while True:
        sep = "&" if "?" in url else "?"
        page_url = f"{url}{sep}page={page}"
        data, resp_headers = http_get_json(page_url, headers)
        if not isinstance(data, list):
            break
        items.extend(data)
        if 'rel="next"' not in resp_headers.get("Link", ""):
            break
        page += 1
    return items


def paginate_gitlab(
    base: str,
    path: str,
    token: str,
    params: dict[str, str] | None = None,
) -> list[Any]:
    items: list[Any] = []
    query = dict(params or {})
    query.setdefault("per_page", "100")
    page = 1
    headers = {"PRIVATE-TOKEN": token, "User-Agent": "count-merged-prs"}

    while True:
        query["page"] = str(page)
        url = f"{base}{path}?{urllib.parse.urlencode(query)}"
        data, resp_headers = http_get_json(url, headers)
        if not isinstance(data, list):
            return data if data is not None else items
        items.extend(data)
        per_page = int(query.get("per_page", "100"))
        next_page = resp_headers.get("X-Next-Page")
        if next_page:
            page = int(next_page)
            continue
        if len(data) < per_page:
            break
        page += 1
    return items


def parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(value, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"Invalid date: {value!r}. Use YYYY-MM-DD or ISO8601.")


def in_range(dt_str: str | None, since: datetime | None, until: datetime | None) -> bool:
    if not dt_str:
        return False
    dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    if since and dt < since:
        return False
    if until and dt > until:
        return False
    return True


def github_api(token: str, host: str = "github.com") -> str:
    return "https://api.github.com" if host == "github.com" else f"https://{host}/api/v3"


def github_headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "count-merged-prs",
    }


def list_github_repos(token: str, org: str, host: str) -> list[str]:
    api = github_api(token, host)
    repos: list[str] = []
    for kind in (f"orgs/{org}/repos", f"users/{org}/repos"):
        batch = paginate_github(f"{api}/{kind}?per_page=100&type=all", token)
        if batch:
            repos = [r["full_name"] for r in batch if not r.get("archived")]
            break
    if not repos:
        raise RuntimeError(f"No GitHub repos found for {org!r}")
    return repos


_GITHUB_MERGED_COUNT_QUERY = """
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    pullRequests(states: MERGED) {
      totalCount
    }
  }
}
"""


def github_graphql(token: str, host: str = "github.com") -> str:
    return "https://api.github.com/graphql" if host == "github.com" else f"https://{host}/api/graphql"


def http_post_json(url: str, headers: dict[str, str], payload: dict[str, Any]) -> Any:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} for {url}: {detail}") from exc


def count_github_merged(
    token: str,
    repo: str,
    host: str,
    since: datetime | None,
    until: datetime | None,
) -> int:
    owner, name = repo.split("/", 1)

    if since is None and until is None:
        data = http_post_json(
            github_graphql(token, host),
            github_headers(token),
            {
                "query": _GITHUB_MERGED_COUNT_QUERY,
                "variables": {"owner": owner, "name": name},
            },
        )
        if data.get("errors"):
            raise RuntimeError(str(data["errors"])[:500])
        repository = (data.get("data") or {}).get("repository")
        if not repository:
            raise RuntimeError(f"Repository not found: {repo}")
        return int(repository["pullRequests"]["totalCount"])

    api = github_api(token, host)
    pulls = paginate_github(
        f"{api}/repos/{owner}/{name}/pulls?state=closed&sort=updated&direction=desc&per_page=100",
        token,
    )
    return sum(
        1 for pr in pulls if pr.get("merged_at") and in_range(pr["merged_at"], since, until)
    )


def github_merged_counts(
    token: str,
    repos: list[str],
    host: str,
    since: datetime | None,
    until: datetime | None,
) -> dict[str, int]:
    return {
        repo: count_github_merged(token, repo, host, since, until)
        for repo in repos
    }


def gitlab_api(host: str = "gitlab.com") -> str:
    host = host.rstrip("/")
    if host.startswith("http://") or host.startswith("https://"):
        base = host
    else:
        base = f"https://{host}"
    return f"{base}/api/v4"


def list_gitlab_projects(token: str, group: str, host: str) -> list[str]:
    api = gitlab_api(host)
    encoded = urllib.parse.quote(group, safe="")
    projects = paginate_gitlab(
        api,
        f"/groups/{encoded}/projects",
        token,
        {"include_subgroups": "true", "archived": "false"},
    )
    if not projects:
        raise RuntimeError(f"No GitLab projects found for group {group!r}")
    return [p["path_with_namespace"] for p in projects]


def count_gitlab_merged(
    token: str,
    project: str,
    host: str,
    since: datetime | None,
    until: datetime | None,
) -> int:
    api = gitlab_api(host)
    encoded = urllib.parse.quote(project, safe="")
    params: dict[str, str] = {"state": "merged", "per_page": "100"}

    if since:
        params["updated_after"] = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    if until:
        params["updated_before"] = until.strftime("%Y-%m-%dT%H:%M:%SZ")

    probe_params = {**params, "per_page": "1"}
    url = f"{api}/projects/{encoded}/merge_requests?{urllib.parse.urlencode(probe_params)}"
    _, headers = http_get_json(url, {"PRIVATE-TOKEN": token, "User-Agent": "count-merged-prs"})
    total = headers.get("X-Total")
    if total is not None:
        return int(total)

    mrs = paginate_gitlab(api, f"/projects/{encoded}/merge_requests", token, params)
    return len(mrs)


def gitlab_merged_counts(
    token: str,
    projects: list[str],
    host: str,
    since: datetime | None,
    until: datetime | None,
) -> dict[str, int]:
    return {
        project: count_gitlab_merged(token, project, host, since, until)
        for project in projects
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Count merged PRs/MRs on GitHub and GitLab",
    )
    parser.add_argument(
        "--github-token",
        default=os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"),
    )
    parser.add_argument(
        "--gitlab-token",
        default=os.environ.get("GITLAB_TOKEN") or os.environ.get("GLAB_TOKEN"),
    )
    parser.add_argument("--github-host", default=os.environ.get("GITHUB_HOST", "github.com"))
    parser.add_argument("--gitlab-host", default=os.environ.get("GITLAB_HOST", "gitlab.com"))

    parser.add_argument("--github-repo", action="append", default=[], help="owner/repo")
    parser.add_argument("--github-org", action="append", default=[], help="GitHub org or user")
    parser.add_argument("--gitlab-project", action="append", default=[], help="group/project")
    parser.add_argument("--gitlab-group", action="append", default=[], help="GitLab group")

    parser.add_argument("--since", help="Only count merges on/after YYYY-MM-DD")
    parser.add_argument("--until", help="Only count merges on/before YYYY-MM-DD")
    parser.add_argument("--json", action="store_true", help="Print JSON output")
    args = parser.parse_args()

    since = parse_date(args.since)
    until = parse_date(args.until)

    github_repos = list(args.github_repo)
    gitlab_projects = list(args.gitlab_project)

    if args.github_token:
        for org in args.github_org:
            github_repos.extend(list_github_repos(args.github_token, org, args.github_host))
    elif args.github_repo or args.github_org:
        print("Error: GITHUB_TOKEN (or --github-token) required for GitHub.", file=sys.stderr)
        return 1

    if args.gitlab_token:
        for group in args.gitlab_group:
            gitlab_projects.extend(list_gitlab_projects(args.gitlab_token, group, args.gitlab_host))
    elif args.gitlab_project or args.gitlab_group:
        print("Error: GITLAB_TOKEN (or --gitlab-token) required for GitLab.", file=sys.stderr)
        return 1

    if not github_repos and not gitlab_projects:
        parser.error(
            "Provide at least one of --github-repo, --github-org, --gitlab-project, --gitlab-group"
        )

    github_repos = sorted(set(github_repos))
    gitlab_projects = sorted(set(gitlab_projects))

    github_counts = (
        github_merged_counts(args.github_token, github_repos, args.github_host, since, until)
        if github_repos
        else {}
    )
    gitlab_counts = (
        gitlab_merged_counts(args.gitlab_token, gitlab_projects, args.gitlab_host, since, until)
        if gitlab_projects
        else {}
    )

    github_total = sum(github_counts.values())
    gitlab_total = sum(gitlab_counts.values())
    grand_total = github_total + gitlab_total

    result = {
        "github": {"repos": github_counts, "total": github_total},
        "gitlab": {"projects": gitlab_counts, "total": gitlab_total},
        "grand_total": grand_total,
        "since": args.since,
        "until": args.until,
    }

    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    print("\nMerged PR/MR counts\n" + "=" * 40)
    if github_counts:
        print("\nGitHub:")
        for repo, count in github_counts.items():
            print(f"  {repo}: {count}")
        print(f"  GitHub subtotal: {github_total}")

    if gitlab_counts:
        print("\nGitLab:")
        for project, count in gitlab_counts.items():
            print(f"  {project}: {count}")
        print(f"  GitLab subtotal: {gitlab_total}")

    print(f"\nGrand total: {grand_total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
