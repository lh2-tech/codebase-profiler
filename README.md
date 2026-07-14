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

That is the only command needed after `tokens` is configured.

## UI features

- **Run analysis** — starts the metadata extraction
- **Progress bar** — shows repository completion while a run is active
- **Repository selection** — optional one-per-line list to limit which repos/projects are processed
- **Open output folder** — opens the run folder on Mac, Windows, or Ubuntu (in Docker, use Download buttons or open `./outputs/raw-extracts` on your computer)
- **Download summary / archive zip** — browser downloads for the completed run

## Modes

### Hosted platform (GitHub / GitLab)

- Token file path in the UI: `/app/tokens`
- Uses the keys in your mounted `tokens` file
- Repositories are cloned inside the container, analysed, then removed before the zip is written

### Already cloned here (offline)

Local repositories live **outside** the Docker image. Mount them with `LOCAL_REPOS_DIR` in `.env`:

```env
LOCAL_REPOS_DIR=/Users/me/customer-repos
```

In the UI, always use the **container path**:

```text
/data/repos
```

Optionally list specific repository folder names in **Repositories to include**.

Do not enter the host path (`/Users/me/...`) — it will not exist inside the container.

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
