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
- **Retry failed repositories** — re-attempt timeout / network / rate-limit failures only
- **Partial summary / archive download** — download `summary.csv` or a partial archive zip while a run is still in progress
- **Download summary / archive zip** — browser downloads for the completed (or partial) run

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

**Merged PRs.** When the platform API reports no merged PRs/MRs for a repository, the count falls back to git history: numbered markers (GitHub merges, Bitbucket "Merged in … (pull request #N)", GitLab "See merge request !N", `(#N)` squash commits) plus every other merge commit. Only the checked-out (default) branch is scanned, so merges made on other branches are not counted.

### Resume / retry (CLI)

Progress is written after every repository to `summary.csv` and `job.json` inside the run folder.

```bash
# Continue pending repos from an interrupted run
python extract_org_raw_data.py --resume outputs/raw-extracts/raw-extract-ORG-STAMP --tokens-file tokens

# Retry only timeout / network / rate-limit failures
python extract_org_raw_data.py --resume outputs/raw-extracts/raw-extract-ORG-STAMP --tokens-file tokens --retry-failed

# Shorter clone timeout (default 300s) and fewer retries
python extract_org_raw_data.py --github-org MyOrg --tokens-file tokens \
  --clone-timeout 180 --clone-retries 1 --workers 4
```

### Already cloned here (offline)

Local repositories live **outside** the Docker image. Put full clones in `./repos`, or set `LOCAL_REPOS_DIR` in `.env`:

```env
LOCAL_REPOS_DIR=/Users/me/customer-repos
```

The UI uses the mounted folder automatically — no path entry needed. Optionally list specific repositories with **Load repositories** / **Repositories to include**.

## Outputs

Results are written to `./outputs/raw-extracts/` on your computer (mounted into the container). Each run produces:

- `summary.csv` (+ `summary.xlsx` when generated)
- metadata JSON under `api/` and `git/`
- a timestamped zip beside the run folder

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
| `count_merged_prs.py` | GitHub/GitLab API helpers |
| `github_app_auth.py` | GitHub App authentication |
| `docker-compose.yml` | One-command startup |
| `Dockerfile` | Self-contained runtime image |
