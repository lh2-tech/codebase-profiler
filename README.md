# Codebase Profiler

Privacy-safe repository evidence extractor with a local browser UI. Analyses GitHub/GitLab organisations, Bitbucket Cloud workspaces or folders of local clones and produces a metadata-only archive (no source code in the output zip).

## Quick start (Docker)

1. Copy the token template into the `secrets/` folder and add your credentials:

```bash
mkdir -p secrets && cp tokens.example secrets/tokens && chmod 600 secrets/tokens
```

Do this **before** the first `docker compose up`. Docker mounts the whole `secrets/` folder rather than the single file: a single-file bind mount pins one inode, so a tokens file created or replaced after the container started leaves the container reading a dead reference and failing with `No such file or directory` even though the file is plainly there on your machine.

2. Optional: copy the environment template if you want to change ports or the offline repos mount:

```bash
cp .env.example .env
```

Edit `.env` to point `LOCAL_REPOS_DIR` at a folder on your computer that contains full local git clones.

3. Start the app:

```bash
docker compose up --build
```

4. Open [http://localhost:8766](http://localhost:8766)

A printable step-by-step guide for non-technical users is in [USER_GUIDE.pdf](USER_GUIDE.pdf).

That is the only command needed after `secrets/tokens` is configured.

## Token setup

Put one line per credential in `secrets/tokens` (`key=value`). The key name is what you type into the UI's token field, so it can be anything — but it must match exactly, or the run fails with `Missing '<name>' in tokens file`.

### GitLab

| | Required |
|---|---|
| Token type | **Classic** personal access token (`glpat-…`) |
| Scopes | `read_api` **and** `read_repository` |
| Role on the group | **Reporter** or higher |

Both scopes are needed and they do different jobs: `read_api` covers every metadata call (groups, projects, merge requests, members, languages), `read_repository` is what lets the container `git clone` over HTTPS. A token with only `read_api` discovers repositories and then fails at the clone step.

Reporter is the minimum useful role — Guest members can see that a private project exists but cannot read its code, so a Guest token produces empty or failed analyses.

> **Avoid fine-grained GitLab tokens.** They are scoped to *selected resources*, not just permission types, so a token can hold the right permission names and still be unable to reach a given group's projects. The tell is a `403` whose body mentions `insufficient_granular_scope` and names the permission it wants — switch to a classic PAT.

### GitHub

| | Required |
|---|---|
| Token type | Classic PAT |
| Scopes | `repo` (private repos + clone) and `read:org` (org and org-repo listing) |

`public_repo` instead of `repo` is enough if you only ever analyse public repositories. For a fine-grained GitHub token the equivalents are Repository → Contents (Read), Metadata (Read), Pull requests (Read), and Organization → Members (Read); classic tokens are still the simpler choice.

GitHub App auth is also supported via `--github-app` (see `tokens.example` for `github_app_id` / `github_app_pem`).

### Bitbucket Cloud

| | Required |
|---|---|
| Token type | Atlassian **scoped API token** (`ATATT…`), or a workspace / repository access token |
| Scopes | `read:repository:bitbucket` and `read:pullrequest:bitbucket` (for a workspace / repository access token: **Repositories: Read** and **Pull requests: Read**) |
| Email | The Atlassian account email, for scoped API tokens only. Not needed for access tokens |
| Workspace | The **slug** (the part after `bitbucket.org/`) — always required |
| Token-file keys | `bitbucket_token`, and `bitbucket_email` for scoped API tokens |

The tool only reads. Every metadata call is accepted with the `repository` and `pullrequest` scopes, and `git clone` needs `read:repository:bitbucket`. A token that also carries `read:project:bitbucket` / `read:workspace:bitbucket` works too; they are not required. The token's account (or the access token's workspace) must be able to see the repositories you want — a token that can reach a workspace but no repositories returns an empty listing rather than an error.

REST and git authenticate differently, and the tool handles both: REST uses `email:token` Basic auth (or a Bearer token when no email is set, as for access tokens), while `git clone` uses the literal username `x-token-auth`. A "You may not have access to this repository" clone error with a valid token usually means the wrong git username was used, not missing access. A `403` quoting `required` / `granted` scopes means authentication worked and only a scope is missing.

Bitbucket Cloud has no account-wide discovery (`/workspaces` is gone and `/repositories` without a workspace returns `410`), so there is no "analyse everything this token can access" option — name the workspace. The API exposes neither a language breakdown nor a contributor list, so those come from the clone (SCC and git authors). Bitbucket Server / Data Center is not supported.

### OpenAI

`openai_key` is only read when LLM mode is enabled (`--llm`, or the checkbox in the UI). Leave it out entirely if you do not use that mode.

### Checking a token before a run

```bash
cd ~/DataLabs/codebase-profiler && TOKEN=$(grep '^YOUR_KEY_NAME=' secrets/tokens | cut -d= -f2-) && curl -s -o /dev/null -w '%{http_code}\n' -H "PRIVATE-TOKEN: $TOKEN" "https://gitlab.com/api/v4/groups/YOUR_GROUP/projects?include_subgroups=true&per_page=1"
```

`200` with a non-empty body is a working token. `403` names the missing permission in the response body. `200` with `[]` means GitLab found nothing to return — check that the account is a member of the group at Reporter or above, and that the projects actually live in the group you queried.

### Endpoints used

Useful when a security team asks what the token is actually allowed to touch. All calls are `GET`; the tool never writes.

- **GitLab** — `/groups`, `/groups/:id/projects`, `/projects`, `/projects/:id`, `/projects/:id/languages`, `/projects/:id/members/all`, `/projects/:id/merge_requests` (plus `/:iid`, `/notes`, `/changes`), and `git clone` over HTTPS
- **Bitbucket** — `/2.0/repositories/:workspace`, `/2.0/repositories/:workspace/:repo`, `/2.0/repositories/:workspace/:repo/pullrequests?state=MERGED`, and `git clone` over HTTPS
- **GitHub** — `/user`, `/user/orgs`, `/user/repos`, `/orgs/:org/repos`, `/repos/:full_name`, `/repos/:full_name/languages`, `/repos/:full_name/contributors`, `/repos/:owner/:name/pulls`, and `git clone` over HTTPS

## UI features

- **Run analysis** — starts the metadata extraction
- **Progress bar** — shows repository completion while a run is active, including failure classes (timeout, rate_limit, network, …)
- **Organisation / group discovery** — load orgs/groups the token belongs to
- **Bitbucket workspace** — enter a workspace slug and load its repositories (Bitbucket cannot list workspaces)
- **Accessible GitHub repositories** — load every repo the token can access (owner, collaborator, and org member), including direct invites outside org membership
- **Manual repository list** — paste `owner/repo` lines when discovery still misses a target
- **Repository selection** — optional picker to limit which repos/projects are processed
- **Resume previous run** — continue an interrupted job from `job.json` / `summary.csv` without redoing completed repos
- **Automatic retry of failed repositories** — no button: repos that failed on a timeout, network or rate-limit error are retried at the end of every run, and the run only finishes after that (see [Clone timeouts and automatic retry](#clone-timeouts-and-automatic-retry))
- **Partial summary / archive download** — download `summary.csv` or a partial archive zip while a run is still in progress
- **Download summary / archive zip** — browser downloads for the completed (or partial) run
- **Duplicate commits (duplicate %)** — always on for runs of 2 or more repos, no checkbox; see [Duplicate commits](#duplicate-commits-duplicatescsv)

## Modes

### Hosted platform (GitHub / GitLab / Bitbucket)

- Put credentials in the host `secrets/tokens` file (mounted into the container automatically)
- In the UI, choose the token key name (for example `github-data-token`) — no path entry needed
- Repositories are cloned inside the container, analysed, then removed before the zip is written
- See [Token setup](#token-setup) for the exact scopes each platform needs
- Large orgs: prefer selecting a subset of repos, or use clone-then-offline (`clone_all_repos.py` + offline mode) if the platform run hits API rate limits
- CLI equivalent for direct-access repos:

```bash
python extract_org_raw_data.py --github-accessible --tokens-file tokens --github-token-name data-lh2-github-token
# or specific repos:
python extract_org_raw_data.py --github-repo owner/repo-one --github-repo owner/repo-two --tokens-file tokens
```

Bitbucket needs a workspace slug (pass `--bitbucket-email-name ""` for access tokens that have no email):

```bash
python extract_org_raw_data.py --bitbucket-workspace my-workspace --tokens-file tokens
python extract_org_raw_data.py --bitbucket-repo my-workspace/repo-one --tokens-file tokens
python clone_all_repos.py --bitbucket-workspace my-workspace   # clones into <repos-root>/bitbucket/<workspace>/
```

**Measured branch.** For cloned repos (GitHub, GitLab, Bitbucket) every git/LOC/language metric is read from the remote branch with the newest commit, not the platform default, and the choice is recorded in the `measured_branch` column (`default_branch` stays as the API reports it). Ties go to the default branch. Set `EXTRACT_USE_DEFAULT_BRANCH=1` to restore the old default-branch behaviour. Local-path targets (`--local-repos-dir`) are measured as checked out, unless `EXTRACT_LOCAL_LATEST_BRANCH=1` is set: then the same newest-branch rule applies to them (a clone of an export uses its remote-tracking branches, a repo without a remote its own branches) and the repo's HEAD is moved to that branch, which is why it is opt-in. `commits_default_branch` then counts the default branch's own history. `EXTRACT_LOCAL_BRANCH_MAP=<file>` (JSON `{local repo directory name: branch}`, names case-insensitive) makes a local repo be measured on the branch the client named: it is checked out if it exists (own branch, else `origin/`), wins over the newest-branch rule, and is recorded in `measured_branch`. A missing branch, unreadable or malformed file falls back to the normal rule.

**Merged PRs.** When the platform API reports no merged PRs/MRs for a repository, the count falls back to git history: numbered markers (GitHub merges, Bitbucket "Merged in … (pull request #N)", GitLab "See merge request !N", `(#N)` squash commits) plus every other merge commit. Only the checked-out (default) branch is scanned, so merges made on other branches are not counted.

### PR-shape and code-structure columns

Aggregate-only columns added to `summary.csv` (implemented in `code_metrics.py`):

| Column | Meaning |
|--------|---------|
| `pr_rich_pct` | Merged PRs with a linked issue **and** a substantive human review comment (inline review comment, or a review/note body of 20+ words) |
| `pr_simple_pct` | 1-2 files changed, no human discussion (and not rich) |
| `pr_standard_pct` | 3-10 files changed (and not rich) |
| `pr_other_pct` | Everything else, so the four sum to 100 |
| `pr_tiers_classified` | PRs that had enough API data to classify |
| `function_count`, `class_count` | Named functions/methods and classes/interfaces/structs/enums/traits, via tree-sitter, production code only |
| `docstring_coverage_pct` | Functions with a docstring (Python) or adjacent doc comment (other languages) |
| `comment_docstring_ratio` | SCC comment lines / (comment + code) over the same production source files as `function_count` (tests, vendored/generated code, Markdown/JSON/YAML excluded); SCC counts docstrings as comments |
| `untested_files`, `untested_files_pct` | Heuristic: production files that no test imports (imports are parsed and resolved for Python, JS/TS, Java/Kotlin/Scala) and that have no same-named test in a mirrored directory (generic stems like `models`/`utils`/`index` need the directories to line up). Not coverage; other languages use name matching only |

PR tiers need the platform API: GitHub and GitLab only (GitLab adds one `closes_issues` call per MR). Bitbucket, git-history-only and local runs leave them blank. Tiers are applied Rich > Simple > Standard. Per-PR tiers are written to `api/<repo>/pr_tiers.json`.

### Resume / retry (CLI)

Progress is written after every repository to `summary.csv` and `job.json` inside the run folder.

```bash
# Continue pending repos from an interrupted run
python extract_org_raw_data.py --resume outputs/raw-extracts/raw-extract-ORG-STAMP --tokens-file tokens

# Shorter clone timeout (default 300s) and fewer retries
python extract_org_raw_data.py --github-org MyOrg --tokens-file tokens \
  --clone-timeout 180 --clone-retries 1 --workers 4
```

### Clone timeouts and automatic retry

- **Clone attempts.** Each clone gets `--clone-timeout` seconds (default 300) and `--clone-retries` extra attempts (default 2, so 3 tries). If the last try ends in a timeout or network error, one final **extended** try runs with `--clone-extended-timeout` seconds (default 1200; `0` turns it off). If that fails too, the repo is recorded as failed and the run moves on. Not-found, permission and disk-full errors end at the first attempt.
- **Retry at the end of the run.** After all repos were processed, every repo that failed on a `timeout`, `network` or `rate_limit` error is processed again (`--end-retry-passes`, default 1; `0` turns it off), and the run only finishes, writes `summary.csv` and the zip after that. `not_found`, `auth` and unclassified errors are not retried. A resumed run also retries such failures it carried over. `--resume --retry-failed` still exists for the CLI to retry them at the start instead.

### Already cloned here (offline)

Local repositories live **outside** the Docker image. Put full clones in `./repos`, or set `LOCAL_REPOS_DIR` in `.env`:

```env
LOCAL_REPOS_DIR=/Users/me/customer-repos
```

The UI uses the mounted folder automatically — no path entry needed. Optionally list specific repositories with **Load repositories** / **Repositories to include**.

## Outputs

Results are written to `./outputs/raw-extracts/` on your computer (mounted into the container). Each run produces:

- `summary.csv` (+ `summary.xlsx` when `openpyxl` is installed)
- `for-check.csv`, also the second sheet (`for-check`) of `summary.xlsx` — platform-side facts for the checker (see below)
- `duplicates.csv` — which repos of the run share commits (see [Duplicate commits](#duplicate-commits-duplicatescsv)); only when the comparison ran
- metadata JSON under `api/` and `git/`, including `git/<org__repo>/commit_hashes.txt.gz` (commit hashes only)
- a timestamped zip beside the run folder

```
summary.csv  duplicates.csv  for-check.csv  job.json  manifest.json  repos.json
api/<org__repo>/    repo.json languages.json contributors.json merged_prs.json ...
git/<org__repo>/    git_stats.json commits.jsonl scc.json ... commit_hashes.txt.gz
logs/
```

### Duplicate commits (duplicates.csv)

For one run, the tool compares the **commit hashes** of the repos it processed and reports, per repo, how much of its history also exists in at least one *other* repo of the run: a copy, a fork, a mirror, a repo split off another.

- `commits(A)` is every hash reachable from any ref of repo A (`git rev-list --all`). It is complete: `EXTRACT_COMMIT_DETAIL_LIMIT` only trims `commits.jsonl`.
- `summary.csv` gets three columns, appended after all existing ones:

| Column | Meaning |
|---|---|
| `duplicate_pct` | 100 x commits of this repo that also exist in another repo of the run / commits of this repo, half-up to 1 decimal. `0.0` means compared and nothing shared |
| `duplicate_commits` | the numerator: how many of this repo's commits exist in at least one other repo (a commit held by three repos is counted once) |
| `duplicates_with` | the top 3 other repos as `name:pct_of_this_repo`, joined by `; `, by shared commits descending then name. `name` is the repo's full name as in `repos.json` |

  The three are **blank** (unknown, never 0) for a repo with no commits, a repo whose hashes could not be collected (failed repo, `git rev-list` error or timeout, a SHA-256 repository), and for every repo when fewer than two repos have commits. They are blank while the run is in progress and are filled once, at the end.
- `duplicates.csv` (run folder root, also in the zip): `repo_a,repo_b,shared_commits,pct_of_a,pct_of_b`, one row per unordered pair that shares at least one commit, sorted by `shared_commits` descending then `repo_a`, `repo_b` (`repo_a` sorts before `repo_b`). Header only when nothing is shared. A pair is two repos that share history; it does not say which one is the original.
- One log line closes the comparison: `Duplicate commits: N of M repos share commits with another repo (duplicates.csv)` (M = repos with commits that were compared) or `Duplicate commits: none found`.
- **On/off.** On by default when the run has 2 or more repos. `--no-dedup` (CLI only; the UI always compares) skips it: no `duplicates.csv`, no hash files, and the three columns stay in `summary.csv` but blank, so the column layout never changes. On `--resume` the same flag clears values written by an earlier run.
- **Resume.** The clone is deleted after each repo, so each repo's hashes are saved while it exists, in `git/<org__repo>/commit_hashes.txt.gz` (one 40-hex hash per line, sorted, gzip). The comparison runs once at the end over every repo of the run that has such a file, so `--resume` / `--retry-failed` produce the full result, not only the repos that were re-run. A repo that fails loses its hash file and is blank.
- **Privacy.** `commit_hashes.txt.gz` holds hashes and nothing else: no messages, authors, paths or content (`test_no_source_in_archive.py` asserts every line is 40 lowercase hex characters). A hash is not secret, but it does let someone who already has a repository check whether it is a copy of yours.
- **Limits and cost.** Memory is about 100 bytes per distinct commit hash across the whole run (an index of 20-byte keys), so 1M distinct commits is roughly 100 MB and 10M roughly 1 GB; the work is linear in the total number of commits, not quadratic in the number of repos. `rev-list --all` follows the existing `EXTRACT_GIT_LOG_TIMEOUT` and git hardening. Hosted-platform clones carry branches and tags, not PR/MR refs (`refs/pull/*`), so a commit that only exists in a PR is not counted. Two repos that independently made a commit with identical content, author, date and parent get the same hash and so count as sharing it. A corrupt `commit_hashes.txt.gz` makes the whole comparison fail: it is logged, the columns stay blank and `duplicates.csv` is not written.

### for-check.csv

An extract cannot say what it *should* contain. For GitHub/GitLab repos the tool therefore asks the platform for facts
that git history cannot give back, just before and just after each repo is processed, and records them in long format
(`repo,section,key,value`):

| Section | Content |
|---|---|
| `meta` | platform, host, started/finished timestamps |
| `counts_before` / `counts_after` | PRs/MRs, issues, labels, milestones, releases, branches, tags, commits, comments, collaborators, size |
| `ref_before` / `ref_after` | branches, tags, PR/MR head refs with sha (`git ls-remote`; the token travels in the environment, not argv). `ref_after` lists only refs that changed since `ref_before` (`deleted` = gone) |
| `captured` | what this tool recorded: commits, `measured_branch`, `commits_default_branch`, merged PRs and `merged_prs_source`, contributors, default branch, size |
| `file` | every file written under `api/<repo>/` and `git/<repo>/`, with size |
| `error` | anything that could not be collected (status or category only, never response bodies) |

Unknown stays blank (e.g. collaborators need admin, or GitLab hides a count above 10,000 items); collection never fails
the extract. Bitbucket gets refs only; local/offline clones get `captured` only (the skip is noted in `meta`). Skip the
whole thing with `--no-for-check`. Per-repo facts live in `api/<repo>/for_check.json`, so `--resume` keeps them; repos
that are not part of the current run are left out of the CSV.

Reading the numbers (what is and is not comparable):

- **Commits.** `counts_*.commits` is the platform's default-branch history. `captured.commits` is the *measured* branch
  (the most recently committed one), so compare `counts_*.commits` with `captured.commits_default_branch`; the two
  `captured` commit numbers only match when `measured_branch` equals `default_branch`.
- **Merged PRs.** `counts_*.prs` is every PR/MR; `counts_*.merged_prs` is merged only and is the one to compare with
  `captured.merged_prs`. When `merged_prs_source` is `git`, the figure came from merge/squash markers in history
  (platform API returned zero, or local run) and will undercount squash/rebase workflows.
- **People.** `collaborators` (platform members, needs admin) and `contributors` (authors recorded by the tool) are
  different populations: do not expect them to be equal.
- **Before snapshot.** To halve the API budget, `counts_before` is a light snapshot (`counts_before_scope=light`):
  comment counts, collaborators and (GitLab) labels/milestones/releases are only taken in `counts_after`.

Privacy: branch and tag names can carry client names, ticket ids or secrets, so by default they are replaced by a
digest (`refs/heads/h-<12 hex>`; sha and ref type are kept, PR/MR head refs are only numbers). `--for-check-refs names`
writes them verbatim, `--for-check-refs none` writes no ref rows. The digest is unsalted: it hides names from a reader
but a short guessable name (`main`, `develop`) can be recomputed. `summary.xlsx` holds at most 1,000,000 for-check rows
(the CSV keeps everything); cells starting with `= + - @` are stored as text.

Every git command run against a client repo disables repo-supplied hooks and `core.fsmonitor`, so a hook shipped in an
archive cannot execute. `--github-host` / `--gitlab-host` must be a bare host (`host[:port]`; GitLab also accepts
`http(s)://`), `http://` logs a cleartext warning, and credentials are dropped if an API redirect leaves the host.

### Compatibility with Squirrel

Squirrel (`stages.py`) reads `summary.csv` by header name (`loc`, `commits`, `merged_prs`) and passes `--no-for-check`.
`summary.csv` gains a `subgroup` column right after `org` (GitLab: the path between group and repo, e.g. `mobile-apps` or `a/b`; Bitbucket: the project name; GitHub: blank), so same-named repos in different subgroups keep separate rows. Squirrel reads by header name, so this does not affect it. Existing columns are otherwise untouched (the three duplicate columns are only appended at the end) and `--no-for-check` skips both `for-check.csv` and the xlsx sheet.
`--no-for-check` and `--for-check-refs` exist only in profilers that include for-check; an older profiler rejects the
flag (argparse "unrecognized arguments"), so pin Squirrel to a profiler version that has it.

## Local development (without Docker)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
brew install scc   # or install scc another way
cp tokens.example tokens
python extract_org_raw_data.py --ui
```

## Requirements

- `git`
- `scc` (LOC / language metrics)
- Python 3.11+ (included in the Docker image)

## Security notes

- Never commit `secrets/`, `tokens`, `.env`, or `*.pem` files
- The UI binds to localhost on your machine via Docker port mapping (`8766:8766`)
- LLM mode sends bounded code excerpts to OpenAI; the API key is not stored in output archives

## Repository layout

| File | Purpose |
|------|---------|
| `extract_org_raw_data.py` | CLI extractor |
| `extract_ui.py` | Browser UI |
| `commit_dedup.py` | Commit-hash comparison between repos: hash files, duplicate % columns, `duplicates.csv` |
| `count_merged_prs.py` | GitHub/GitLab API helpers |
| `for_check.py` | for-check.csv: platform-side counts/refs + summary.xlsx writer |
| `github_app_auth.py` | GitHub App authentication |
| `docker-compose.yml` | One-command startup |
| `Dockerfile` | Self-contained runtime image |
