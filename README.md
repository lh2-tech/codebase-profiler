# Codebase Profiler

Privacy-safe repository evidence extractor with a local browser UI. Analyses GitHub/GitLab organisations or folders of local clones and produces a metadata-only archive (no source code in the output zip).

## Quick start (Docker)

1. Copy the token template and add your credentials:

```bash
cp tokens.example tokens
```

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

That is the only command needed after `tokens` is configured.

## UI features

- **Run analysis** — starts the metadata extraction
- **Progress bar** — shows repository completion while a run is active, including failure classes (timeout, rate_limit, network, …)
- **Organisation / group discovery** — load orgs/groups the token belongs to
- **Accessible GitHub repositories** — load every repo the token can access (owner, collaborator, and org member), including direct invites outside org membership
- **Manual repository list** — paste `owner/repo` lines when discovery still misses a target
- **Repository selection** — optional picker to limit which repos/projects are processed
- **Resume previous run** — continue an interrupted job from `job.json` / `summary.csv` without redoing completed repos
- **Retry failed repositories** — re-attempt timeout / network / rate-limit failures only
- **Partial summary / archive download** — download `summary.csv` or a partial archive zip while a run is still in progress
- **Download summary / archive zip** — browser downloads for the completed (or partial) run

## Modes

### Hosted platform (GitHub / GitLab)

- Put credentials in the host `tokens` file (mounted into the container automatically)
- In the UI, choose the token key name (for example `github-data-token`) — no path entry needed
- Repositories are cloned inside the container, analysed, then removed before the zip is written
- GitHub PAT needs the `repo` scope to see private collaborator repositories
- Large orgs: prefer selecting a subset of repos, or use clone-then-offline (`clone_all_repos.py` + offline mode) if the platform run hits API rate limits
- CLI equivalent for direct-access repos:

```bash
python extract_org_raw_data.py --github-accessible --tokens-file tokens --github-token-name data-lh2-github-token
# or specific repos:
python extract_org_raw_data.py --github-repo owner/repo-one --github-repo owner/repo-two --tokens-file tokens
```

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

- Never commit `tokens`, `.env`, or `*.pem` files
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
