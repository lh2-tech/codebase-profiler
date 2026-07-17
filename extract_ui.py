#!/usr/bin/env python3
"""Local, dependency-free browser UI for extract_org_raw_data.py."""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import webbrowser
from csv import reader
from datetime import datetime, timezone
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parent
EXTRACTOR = ROOT / "extract_org_raw_data.py"
DEFAULT_OUTPUT = ROOT / "outputs" / "raw-extracts"
STATE_FILE = DEFAULT_OUTPUT / ".ui_state.json"
LOGO_PATH = ROOT / "LH2-DataLabs.svg"
CSRF_TOKEN = secrets.token_urlsafe(32)

from extract_org_raw_data import (  # noqa: E402
    discover_local_repositories,
    list_github_accessible_repos,
    list_github_orgs_for_token,
    list_github_repos_for_org,
    list_gitlab_groups_for_token,
    list_gitlab_projects_for_group,
    parse_tokens_file,
)

STATE: dict[str, Any] = {
    "phase": "idle",
    "running": False,
    "command": [],
    "log": [],
    "returncode": None,
    "summary_path": None,
    "run_dir": None,
    "zip_path": None,
    "xlsx_path": None,
    "output_dir": None,
    "started_at": None,
    "finished_at": None,
    "repos_ok": None,
    "repos_failed": None,
    "form_settings": None,
    "last_error": None,
}
LOCK = threading.Lock()


def _empty_state() -> dict[str, Any]:
    return {
        "phase": "idle",
        "running": False,
        "command": [],
        "log": [],
        "returncode": None,
        "summary_path": None,
        "run_dir": None,
        "zip_path": None,
        "xlsx_path": None,
        "output_dir": str(DEFAULT_OUTPUT.resolve()),
        "started_at": None,
        "finished_at": None,
        "repos_ok": None,
        "repos_failed": None,
        "form_settings": None,
        "last_error": None,
    }


def extract_selected_repos(fields: dict[str, list[str]]) -> list[str]:
    return [value.strip() for value in fields.get("selected_repos", []) if value.strip()]


def merge_repo_selectors(*groups: list[str]) -> list[str]:
    """Merge repo selectors preserving order and dropping duplicates."""
    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for item in group:
            key = item.strip()
            if not key:
                continue
            lowered = key.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            merged.append(key)
    return merged


def extract_form_settings(fields: dict[str, list[str]]) -> dict[str, Any]:
    llm_enabled = fields.get("llm_enabled", [""])[0] == "on"
    selected = extract_selected_repos(fields)
    manual = parse_repo_selectors(fields.get("manual_repos", [""])[0])
    return {
        "mode": fields.get("mode", ["offline"])[0],
        "local_repos_dir": fields.get("local_repos_dir", [""])[0].strip(),
        "hosted_platform": fields.get("hosted_platform", ["github"])[0],
        "tokens_file": fields.get("tokens_file", ["tokens"])[0].strip() or "tokens",
        "github_org": fields.get("github_org", [""])[0].strip(),
        "github_token_name": fields.get("github_token_name", ["data-lh2-github-token"])[0].strip()
        or "data-lh2-github-token",
        "gitlab_group": fields.get("gitlab_group", [""])[0].strip(),
        "gitlab_token_name": fields.get("gitlab_token_name", ["gitlab_token"])[0].strip()
        or "gitlab_token",
        "workers": fields.get("workers", ["4"])[0].strip() or "4",
        "llm_enabled": llm_enabled,
        "selected_repos": "\n".join(selected),
        "manual_repos": "\n".join(manual),
        "github_accessible": fields.get("github_accessible", [""])[0] == "on",
    }


def read_token_from_fields(fields: dict[str, list[str]], platform: str) -> str:
    tokens_file = Path(fields.get("tokens_file", ["tokens"])[0].strip() or "tokens")
    if not tokens_file.is_file():
        raise ValueError(f"Tokens file not found: {tokens_file}")
    tokens = parse_tokens_file(tokens_file)
    if platform == "github":
        token_name = fields.get("github_token_name", ["data-lh2-github-token"])[0].strip()
        if token_name not in tokens:
            raise ValueError(f"Missing {token_name!r} in tokens file")
        return tokens[token_name]
    token_name = fields.get("gitlab_token_name", ["gitlab_token"])[0].strip()
    if token_name not in tokens:
        raise ValueError(f"Missing {token_name!r} in tokens file")
    return tokens[token_name]


def parse_repo_selectors(text: str | list[str]) -> list[str]:
    if isinstance(text, list):
        return [line.strip() for line in text if str(line).strip()]
    return [line.strip() for line in str(text).splitlines() if line.strip()]


def is_under_archive(path: Path) -> bool:
    archive_root = DEFAULT_OUTPUT.resolve()
    resolved = path.resolve()
    return resolved == archive_root or archive_root in resolved.parents


def compute_progress(log_lines: list[str]) -> dict[str, Any]:
    total = 0
    completed = 0
    failed = 0
    current = ""
    for line in log_lines:
        match = re.search(r"Extracting (\d+) repos", line)
        if match:
            total = int(match.group(1))
        if " OK " in line:
            completed += 1
            current = line.split(" OK ", 1)[1].split(":", 1)[0].strip()
        elif "FAIL " in line:
            failed += 1
            current = line.split("FAIL ", 1)[1].split(":", 1)[0].strip()
    done = completed + failed
    percent = int((done / total) * 100) if total else 0
    return {
        "total": total,
        "completed": completed,
        "failed": failed,
        "done": done,
        "percent": percent,
        "current": current,
    }


def open_output_folder(output_dir: Path) -> dict[str, Any]:
    if is_docker_mode():
        host_hint = os.environ.get("HOST_OUTPUT_HINT", "./outputs/raw-extracts")
        return {
            "opened": False,
            "path": str(output_dir),
            "message": (
                f"In Docker, open {host_hint} on your computer, "
                "or use the Download buttons below."
            ),
        }
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(output_dir)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"opened": True, "path": str(output_dir), "message": "Opened output folder."}
    if sys.platform.startswith("win"):
        subprocess.Popen(
            ["explorer", os.path.normpath(str(output_dir))],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return {"opened": True, "path": str(output_dir), "message": "Opened output folder."}
    if shutil.which("xdg-open"):
        subprocess.Popen(
            ["xdg-open", str(output_dir)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return {"opened": True, "path": str(output_dir), "message": "Opened output folder."}
    return {
        "opened": False,
        "path": str(output_dir),
        "message": f"No desktop file manager found. Open this folder manually: {output_dir}",
    }


def load_persisted_state() -> None:
    global STATE
    if not STATE_FILE.exists():
        return
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            STATE.update(data)
            STATE["running"] = False
            if STATE.get("phase") == "running":
                STATE["phase"] = "failed"
    except (OSError, json.JSONDecodeError):
        pass


def persist_state() -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    snapshot = {key: value for key, value in STATE.items() if key != "command"}
    STATE_FILE.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")


def set_state(**updates: Any) -> None:
    with LOCK:
        STATE.update(updates)
        persist_state()


def csv_to_xlsx(csv_path: Path) -> Path | None:
    try:
        import openpyxl
    except ImportError:
        return None
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "summary"
    with csv_path.open(newline="", encoding="utf-8") as handle:
        for row in reader(handle):
            sheet.append(row)
    xlsx_path = csv_path.with_suffix(".xlsx")
    workbook.save(xlsx_path)
    return xlsx_path


def parse_manifest_from_log(log_lines: list[str]) -> dict[str, Any]:
    for line in reversed(log_lines):
        text = line.strip()
        if not text.startswith("{"):
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and "run_dir" in data:
            return data
    return {}


def finalize_run_state(log_lines: list[str], returncode: int) -> None:
    summary_path: Path | None = None
    zip_path: Path | None = None
    run_dir: Path | None = None
    manifest = parse_manifest_from_log(log_lines)

    for line in reversed(log_lines):
        if "Done. summary=" in line:
            candidate = Path(line.split("Done. summary=", 1)[1].strip()).resolve()
            if candidate.is_file() and DEFAULT_OUTPUT.resolve() in candidate.parents:
                summary_path = candidate
                run_dir = candidate.parent
            break
    for line in reversed(log_lines):
        if "Zip=" not in line:
            continue
        candidate = Path(line.split("Zip=", 1)[1].strip()).resolve()
        if candidate.is_file() and DEFAULT_OUTPUT.resolve() in candidate.parents:
            zip_path = candidate
            break

    if manifest.get("run_dir"):
        run_dir = Path(manifest["run_dir"])
    if manifest.get("zip"):
        zip_path = Path(manifest["zip"])

    xlsx_path = csv_to_xlsx(summary_path) if summary_path else None
    phase = "completed" if returncode == 0 else "failed"
    if summary_path and returncode != 0:
        phase = "failed"

    set_state(
        phase=phase,
        running=False,
        returncode=returncode,
        summary_path=str(summary_path) if summary_path else None,
        run_dir=str(run_dir) if run_dir else None,
        zip_path=str(zip_path) if zip_path else None,
        xlsx_path=str(xlsx_path) if xlsx_path else None,
        output_dir=str(DEFAULT_OUTPUT.resolve()),
        finished_at=datetime.now(timezone.utc).isoformat(),
        repos_ok=manifest.get("ok"),
        repos_failed=manifest.get("failed"),
        log=log_lines[-500:],
    )


def add_log(line: str) -> None:
    with LOCK:
        STATE["log"].append(line.rstrip())
        STATE["log"] = STATE["log"][-500:]
        persist_state()


def run_extraction(command: list[str], env_overrides: dict[str, str] | None = None) -> None:
    try:
        set_state(
            phase="running",
            running=True,
            command=command,
            log=[],
            returncode=None,
            summary_path=None,
            run_dir=None,
            zip_path=None,
            xlsx_path=None,
            output_dir=str(DEFAULT_OUTPUT.resolve()),
            started_at=datetime.now(timezone.utc).isoformat(),
            finished_at=None,
            repos_ok=None,
            repos_failed=None,
            last_error=None,
        )
        add_log("Starting analysis…")
        proc = subprocess.Popen(
            command,
            cwd=str(ROOT),
            env={**os.environ, **(env_overrides or {})},
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            add_log(line)
        returncode = proc.wait()
        with LOCK:
            log_lines = list(STATE["log"])
        finalize_run_state(log_lines, returncode)
        add_log(f"Finished with exit code {returncode}.")
    except Exception as exc:
        set_state(
            phase="failed",
            running=False,
            returncode=1,
            finished_at=datetime.now(timezone.utc).isoformat(),
        )
        add_log(f"Unable to start extraction: {exc}")


def is_docker_mode() -> bool:
    return os.environ.get("EXTRACT_UI_DOCKER", "").strip().lower() in {"1", "true", "yes"}


def page() -> str:
    docker_mode = is_docker_mode()
    default_local = os.environ.get(
        "DEFAULT_LOCAL_REPOS_DIR",
        "/data/repos" if docker_mode else "",
    )
    default_tokens = os.environ.get(
        "DEFAULT_TOKENS_FILE",
        "/app/tokens" if docker_mode else "tokens",
    )
    local_placeholder = "/data/repos" if docker_mode else "/Users/me/Repositories"
    docker_notice = ""
    if docker_mode:
        docker_notice = (
            '<p class="notice"><strong>Docker mode:</strong> use container paths such as '
            '<code>/data/repos</code> for offline clones and <code>/app/tokens</code> for the '
            "token file. Results are written to the mounted <code>./outputs</code> folder on "
            "your computer. Use Download buttons or open <code>outputs/raw-extracts</code> on "
            "your computer."
            "</p>"
        )
    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Repository Evidence Extractor</title>
<link rel="icon" href="/logo.svg" type="image/svg+xml">
<style>
  :root { color-scheme: light; font-family: Inter, system-ui, sans-serif; color:#172033; background:#f4f7fb; }
  body { margin:0; } main { max-width:900px; margin:0 auto; padding:34px 22px 54px; }
  .brand { display:flex; align-items:center; gap:18px; margin-bottom:8px; }
  .brand-logo { height:48px; width:auto; flex-shrink:0; }
  h1 { margin:0; font-size:28px; } .lead { color:#536078; margin:8px 0 28px; }
  .req { color:#b91c1c; margin-left:3px; }
  input.invalid, select.invalid, textarea.invalid { border-color:#b91c1c; box-shadow:0 0 0 2px #fecaca; }
  .card { background:#fff; border:1px solid #dce3ef; border-radius:14px; padding:22px; margin:16px 0; box-shadow:0 2px 10px #1820330a; }
  .choices { display:grid; grid-template-columns:repeat(auto-fit,minmax(215px,1fr)); gap:10px; }
  label.choice { border:1px solid #dce3ef; border-radius:10px; padding:13px; display:block; cursor:pointer; }
  label.choice:has(input:checked) { border-color:#2563eb; background:#eff6ff; }
  .small { font-size:13px; color:#66758d; display:block; margin-top:5px; }
  label.field { display:block; font-weight:600; margin:13px 0 5px; }
  input, select { box-sizing:border-box; width:100%; padding:10px; border:1px solid #bdc9dc; border-radius:8px; font:inherit; background:#fff; }
  input[type="checkbox"], input[type="radio"] { width:auto; padding:0; margin:0 7px 0 0; vertical-align:middle; }
  .hidden { display:none; } button { border:0; border-radius:8px; background:#1d4ed8; color:#fff; font-weight:700; padding:11px 17px; cursor:pointer; margin-top:18px; }
  button:disabled { background:#97a5bb; cursor:not-allowed; } .notice { color:#536078; font-size:14px; }
  pre { white-space:pre-wrap; word-break:break-word; background:#111827; color:#d1fae5; border-radius:9px; padding:15px; min-height:160px; max-height:420px; overflow:auto; }
  .status { font-weight:700; } .good { color:#15803d; } .bad { color:#b91c1c; }
  .warning { color:#b91c1c; font-weight:700; font-size:14px; margin-top:10px; }
  .csv-scroll { max-height:560px; overflow:auto; margin-top:12px; border:1px solid #dce3ef; border-radius:9px; }
  .csv-scroll table { margin:0 !important; min-width:max-content; }
  .csv-scroll th { position:sticky; top:0; background:#eff6ff; }
  .actions { display:flex; flex-wrap:wrap; gap:10px; margin-top:12px; }
  .actions button { margin-top:0; }
  button.secondary { background:#fff; color:#1d4ed8; border:1px solid #93c5fd; }
  a.button-link { display:inline-block; border-radius:8px; background:#fff; color:#1d4ed8; border:1px solid #93c5fd; font-weight:700; padding:11px 17px; text-decoration:none; }
  a.button-link.disabled { pointer-events:none; opacity:.5; }
  .paths { font-size:13px; color:#536078; margin-top:10px; line-height:1.5; }
  .paths code { font-size:12px; word-break:break-all; }
  .form-error { color:#b91c1c; font-weight:600; font-size:14px; margin-top:10px; }
  .run-meta { font-size:13px; color:#536078; margin-top:6px; }
  .progress-wrap { margin-top:12px; height:10px; background:#e5e7eb; border-radius:999px; overflow:hidden; }
  #progress-bar { height:100%; width:0%; background:#2563eb; transition:width .4s ease; }
  #progress-label { font-size:13px; color:#536078; margin-top:6px; }
  textarea { box-sizing:border-box; width:100%; padding:10px; border:1px solid #bdc9dc; border-radius:8px; font:inherit; background:#fff; min-height:88px; resize:vertical; }
  .inline-actions { display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin:8px 0 12px; }
  .inline-actions button { margin-top:0; }
  .repo-picker { max-height:240px; overflow:auto; border:1px solid #dce3ef; border-radius:9px; padding:10px; background:#fafcff; }
  .repo-option { display:flex; align-items:flex-start; gap:8px; padding:6px 4px; font-size:14px; }
  .repo-option input { margin-top:3px; }
  .picker-empty { color:#66758d; font-size:14px; padding:8px 4px; }
  .picker-loading { color:#2563eb; font-size:14px; padding:8px 4px; font-weight:600; }
  button.is-loading { opacity:.8; cursor:wait; }
</style></head><body><main>
<header class="brand">
  <img src="/logo.svg" alt="LH2 AI Labs" class="brand-logo">
  <div>
    <h1>Repository Evidence Extractor</h1>
    <p class="lead">Creates a metadata archive for later analysis. Source code is analysed locally and is not included in the output zip; commit, pull-request, and issue metadata may be retained.</p>
  </div>
</header>
__DOCKER_NOTICE__
<form id="extract-form" method="post" action="/start">
<input type="hidden" name="csrf_token" value="__CSRF_TOKEN__">
<div class="card"><strong>Where are the repositories?</strong>
<div class="choices">
  <label class="choice"><input type="radio" name="mode" value="offline" checked> <strong>Already cloned here</strong><span class="small">Runs entirely offline. No internet connection is needed.</span></label>
  <label class="choice"><input type="radio" name="mode" value="hosted"> <strong>Hosted platform</strong><span class="small">Connect to a GitHub or GitLab organisation with a token file.</span></label>
</div></div>
<div class="card">
  <div id="offline-fields"><label class="field">Folder holding full local clones<span class="req">*</span></label><input name="local_repos_dir" id="local-repos-dir" value="__DEFAULT_LOCAL_REPOS_DIR__" placeholder="__LOCAL_PLACEHOLDER__" required><div class="inline-actions"><button type="button" id="load-local-repos" class="secondary">Load repositories</button></div><p class="notice"><strong>How to prepare this folder:</strong> use a normal full clone for every repository, for example <code>git clone https://github.com/OWNER/REPO.git</code>. Do not use <code>--depth</code>, because the extractor needs the complete commit history.</p></div>
  <div id="hosted-fields" class="hidden">
    <label class="field">Platform</label><select id="hosted-platform" name="hosted_platform"><option value="github">GitHub</option><option value="gitlab">GitLab</option></select>
    <label class="field">Path to token file<span class="req">*</span></label><input name="tokens_file" value="__DEFAULT_TOKENS_FILE__" placeholder="/path/to/tokens" required>
    <div id="github-fields"><label class="field">GitHub token key<span class="req">*</span></label><input name="github_token_name" value="data-lh2-github-token" placeholder="Key in the token file" required><label class="field">Organisation</label><div class="inline-actions"><button type="button" id="load-github-orgs" class="secondary">Load organisations</button><button type="button" id="load-github-accessible" class="secondary">Load accessible repositories</button></div><select name="github_org" id="github-org-select"><option value="">Choose an organisation (optional if using accessible repos or manual list)</option></select><p class="notice">Organisation listing only shows orgs you belong to. Use <strong>Load accessible repositories</strong> for direct collaborator access, or paste <code>owner/repo</code> names below.</p><label class="choice" style="margin-top:12px;display:flex;align-items:center"><input id="github-accessible" type="checkbox" name="github_accessible"><strong>Analyse every accessible repository</strong><span class="small">Runs against all repos this token can access (owner, collaborator, and org member).</span></label></div>
    <div id="gitlab-fields" class="hidden"><label class="field">GitLab token key<span class="req">*</span></label><input name="gitlab_token_name" value="gitlab_token" placeholder="Key in the token file" required><label class="field">Group<span class="req">*</span></label><div class="inline-actions"><button type="button" id="load-gitlab-groups" class="secondary">Load groups</button></div><select name="gitlab_group" id="gitlab-group-select"><option value="">Choose a group</option></select><p class="notice">GitLab runs are always scoped to one group. Choose a group first, then optionally pick projects from that group below.</p></div>
  </div>
  <div id="manual-repos-wrap" class="hidden">
    <label class="field" id="manual-repos-label">Manual repository list</label>
    <textarea name="manual_repos" id="manual-repos" placeholder="owner/repo-one&#10;owner/repo-two"></textarea>
    <p class="notice" id="manual-repos-help">One <code>owner/repo</code> per line. Use this for repos granted by direct invite that do not appear in the organisation picker.</p>
  </div>
  <div id="repo-picker-wrap" class="hidden">
    <label class="field" id="repo-selection-label">Repositories to include</label>
    <div class="inline-actions"><button type="button" id="select-all-repos" class="secondary">Select all</button><button type="button" id="clear-repos" class="secondary">Clear</button></div>
    <div id="repo-picker" class="repo-picker"><p class="picker-empty">Load repositories to choose which ones to include.</p></div>
    <p class="notice" id="repo-selection-help">Leave all unchecked to include every repository discovered above (organisation mode). For accessible-repo loads, select the repos you want or use Select all.</p>
  </div>
  <label class="field">Parallel workers</label><input name="workers" type="number" value="4" min="1" max="20">
  <label class="choice" style="margin-top:18px;display:flex;align-items:center"><input id="llm-enabled" type="checkbox" name="llm_enabled"><strong>Enable LLM analysis</strong><span class="small">Adds codebase description, industry/domain, vibe-code signals, and repository type.</span></label>
  <div id="llm-fields" class="hidden"><label class="field">OpenAI API key<span class="req">*</span></label><input name="openai_key" type="password" autocomplete="off" placeholder="sk-..."><p id="offline-llm-warning" class="warning hidden">LLM mode requires an internet connection in offline mode.</p></div>
  <button id="start">Create output</button>
  <p id="form-error" class="form-error hidden"></p>
  <p class="notice">The browser interface only listens on this computer. Keep this page open while the analysis runs.</p>
</div></form>
<div class="card"><span id="status" class="status">Ready</span><p id="run-meta" class="run-meta hidden"></p><div id="progress-wrap" class="progress-wrap hidden"><div id="progress-bar"></div></div><p id="progress-label" class="progress-label hidden"></p><pre id="log">No analysis has started.</pre></div>
<div id="results" class="card hidden"><strong>Results</strong>
<div class="actions">
  <button id="open-folder" type="button" class="secondary">Open output folder</button>
  <a id="download-summary" class="button-link secondary" href="#">Download summary</a>
  <a id="download-zip" class="button-link secondary" href="#">Download archive zip</a>
</div>
<p id="artifact-paths" class="paths hidden"></p>
<div class="csv-scroll"><div id="csv-preview" class="notice">Loading summary…</div></div></div>
<script>
const FORM_STORAGE_KEY='extract-ui-form-v4';
const forms = {offline:document.querySelector('#offline-fields'), hosted:document.querySelector('#hosted-fields')};
let savedRepoChecks=new Set();
function updateRepoPickerCopy(){
  const platform=document.querySelector('#hosted-platform').value;
  const mode=document.querySelector('input[name=mode]:checked').value;
  const label=document.querySelector('#repo-selection-label');
  const help=document.querySelector('#repo-selection-help');
  const manualLabel=document.querySelector('#manual-repos-label');
  const manualHelp=document.querySelector('#manual-repos-help');
  if (mode==='hosted' && platform==='gitlab') {
    label.textContent='Projects to include';
    help.textContent='Leave all unchecked to include every project in the selected group.';
    if (manualLabel) manualLabel.textContent='Manual project list';
    if (manualHelp) manualHelp.textContent='Optional. One group/project path per line.';
  } else {
    label.textContent='Repositories to include';
    help.textContent='Organisation mode: leave all unchecked to include every repository in the org. Accessible-repo mode: select the repos you want, or use Select all.';
    if (manualLabel) manualLabel.textContent='Manual repository list';
    if (manualHelp) manualHelp.textContent='One owner/repo per line. Use this for repos granted by direct invite that do not appear in the organisation picker.';
  }
}
function choose(){ const mode=document.querySelector('input[name=mode]:checked').value; Object.entries(forms).forEach(([k,e])=>e.classList.toggle('hidden', k!==mode)); choosePlatform(); chooseLlm(); updateRepoPickerCopy(); }
function choosePlatform(){
  const mode=document.querySelector('input[name=mode]:checked').value;
  const platform=document.querySelector('#hosted-platform').value;
  document.querySelector('#github-fields').classList.toggle('hidden', platform!=='github');
  document.querySelector('#gitlab-fields').classList.toggle('hidden', platform!=='gitlab');
  document.querySelector('#manual-repos-wrap').classList.toggle('hidden', mode!=='hosted');
  if (mode!=='hosted') document.querySelector('#repo-picker-wrap').classList.add('hidden');
  updateRepoPickerCopy();
}
function chooseLlm(){ const enabled=document.querySelector('#llm-enabled').checked; const offline=document.querySelector('input[name=mode]:checked').value==='offline'; document.querySelector('#llm-fields').classList.toggle('hidden', !enabled); document.querySelector('#offline-llm-warning').classList.toggle('hidden', !(enabled && offline)); }
function getSelectedRepos(){ return [...document.querySelectorAll('input[name="selected_repos"]:checked')].map(el=>el.value); }
function getManualRepos(){
  const el=document.querySelector('#manual-repos');
  if (!el) return [];
  return el.value.split(/\\n+/).map(s=>s.trim()).filter(Boolean);
}
function rememberRepoChecks(){ savedRepoChecks=new Set(getSelectedRepos()); }
function setButtonLoading(button, loading, idleText){
  if (!button) return;
  if (!button.dataset.idleText) button.dataset.idleText=idleText||button.textContent;
  button.disabled=loading;
  button.textContent=loading ? 'Loading…' : button.dataset.idleText;
  button.classList.toggle('is-loading', loading);
}
function setRepoPickerControlsEnabled(enabled){
  document.querySelector('#select-all-repos').disabled=!enabled;
  document.querySelector('#clear-repos').disabled=!enabled;
}
function showRepoPickerLoading(message){
  const wrap=document.querySelector('#repo-picker-wrap');
  const picker=document.querySelector('#repo-picker');
  picker.innerHTML='<p class="picker-loading">'+(message||'Loading repositories…')+'</p>';
  wrap.classList.remove('hidden');
  setRepoPickerControlsEnabled(false);
}
function renderRepoPicker(items, emptyMessage){
  const wrap=document.querySelector('#repo-picker-wrap');
  const picker=document.querySelector('#repo-picker');
  setRepoPickerControlsEnabled(true);
  if (!items.length) {
    picker.innerHTML='<p class="picker-empty">'+(emptyMessage||'No repositories found.')+'</p>';
    wrap.classList.remove('hidden');
    return;
  }
  picker.innerHTML='';
  items.forEach(item=>{
    const label=document.createElement('label');
    label.className='repo-option';
    const box=document.createElement('input');
    box.type='checkbox';
    box.name='selected_repos';
    box.value=item.id;
    if (savedRepoChecks.has(item.id)) box.checked=true;
    const text=document.createElement('span');
    text.textContent=item.archived ? item.name+' (archived)' : item.name;
    label.appendChild(box); label.appendChild(text); picker.appendChild(label);
  });
  wrap.classList.remove('hidden');
}
async function postDiscover(path, extra){
  const body=new URLSearchParams({csrf_token:'__CSRF_TOKEN__', ...extra});
  const response=await fetch(path, {method:'POST', headers:{'Content-Type':'application/x-www-form-urlencoded'}, body});
  const payload=await response.json().catch(async()=>({error:await response.text()}));
  if (!response.ok) throw new Error(payload.error||'Discovery failed.');
  return payload;
}
function fillSelect(select, items, placeholder){
  select.innerHTML='';
  const blank=document.createElement('option');
  blank.value=''; blank.textContent=placeholder; select.appendChild(blank);
  items.forEach(item=>{ const opt=document.createElement('option'); opt.value=item.id; opt.textContent=item.name; select.appendChild(opt); });
}
async function loadLocalRepos(){
  showFormError('');
  const button=document.querySelector('#load-local-repos');
  const localDir=document.querySelector('#local-repos-dir').value.trim();
  if (!localDir) { showFormError('Choose the folder holding local clones.'); return; }
  setButtonLoading(button, true, 'Load repositories');
  showRepoPickerLoading('Searching local folders…');
  try {
    const payload=await postDiscover('/discover/local', {local_repos_dir:localDir});
    rememberRepoChecks();
    renderRepoPicker(payload.items, 'No Git repositories found in that folder.');
  } catch (error) {
    document.querySelector('#repo-picker-wrap').classList.add('hidden');
    showFormError(error.message);
  } finally {
    setButtonLoading(button, false);
  }
}
async function loadHostedOrgs(){
  showFormError('');
  const platform=document.querySelector('#hosted-platform').value;
  const button=platform==='github' ? document.querySelector('#load-github-orgs') : document.querySelector('#load-gitlab-groups');
  const idleText=platform==='github' ? 'Load organisations' : 'Load groups';
  const extra={
    hosted_platform:platform,
    tokens_file:document.querySelector('[name=tokens_file]').value,
    github_token_name:document.querySelector('[name=github_token_name]').value,
    gitlab_token_name:document.querySelector('[name=gitlab_token_name]').value,
  };
  setButtonLoading(button, true, idleText);
  try {
    const payload=await postDiscover('/discover/orgs', extra);
    if (platform==='github') fillSelect(document.querySelector('#github-org-select'), payload.items, 'Choose an organisation');
    else fillSelect(document.querySelector('#gitlab-group-select'), payload.items, 'Choose a group');
    document.querySelector('#repo-picker-wrap').classList.add('hidden');
  } catch (error) { showFormError(error.message); }
  finally { setButtonLoading(button, false); }
}
async function loadHostedRepos(){
  showFormError('');
  const platform=document.querySelector('#hosted-platform').value;
  const extra={
    hosted_platform:platform,
    tokens_file:document.querySelector('[name=tokens_file]').value,
    github_token_name:document.querySelector('[name=github_token_name]').value,
    gitlab_token_name:document.querySelector('[name=gitlab_token_name]').value,
  };
  if (platform==='github') {
    extra.github_org=document.querySelector('#github-org-select').value;
    if (!extra.github_org) return;
  } else {
    extra.gitlab_group=document.querySelector('#gitlab-group-select').value;
    if (!extra.gitlab_group) return;
  }
  showRepoPickerLoading('Loading repositories…');
  try {
    const payload=await postDiscover('/discover/repos', extra);
    rememberRepoChecks();
    renderRepoPicker(payload.items, platform==='gitlab' ? 'No projects found in this group.' : 'No repositories found for this selection.');
  } catch (error) {
    document.querySelector('#repo-picker-wrap').classList.add('hidden');
    showFormError(error.message);
  }
}
async function loadAccessibleGithubRepos(){
  showFormError('');
  const button=document.querySelector('#load-github-accessible');
  const extra={
    hosted_platform:'github',
    tokens_file:document.querySelector('[name=tokens_file]').value,
    github_token_name:document.querySelector('[name=github_token_name]').value,
  };
  setButtonLoading(button, true, 'Load accessible repositories');
  showRepoPickerLoading('Loading every repository this token can access…');
  try {
    const payload=await postDiscover('/discover/accessible-repos', extra);
    rememberRepoChecks();
    renderRepoPicker(
      payload.items,
      'No accessible repositories found for this token (owner, collaborator, or org member).'
    );
  } catch (error) {
    document.querySelector('#repo-picker-wrap').classList.add('hidden');
    showFormError(error.message);
  } finally {
    setButtonLoading(button, false);
  }
}
function clearInvalid(){ document.querySelectorAll('.invalid').forEach(el=>el.classList.remove('invalid')); }
function markInvalid(name){ const el=document.querySelector('[name="'+name+'"],#'+name); if (el) el.classList.add('invalid'); }
function validateForm(){
  clearInvalid();
  const data=readFormSettings();
  const errors=[];
  if (data.mode==='offline') {
    if (!data.local_repos_dir.trim()) errors.push(['local-repos-dir','Choose the folder holding local clones.']);
  } else {
    if (!data.tokens_file.trim()) errors.push(['tokens_file','Enter the token file path.']);
    if (data.hosted_platform==='github') {
      if (!data.github_token_name.trim()) errors.push(['github_token_name','Enter the GitHub token key.']);
      const hasOrg=!!data.github_org.trim();
      const hasSelected=getSelectedRepos().length>0;
      const hasManual=getManualRepos().length>0;
      const hasAccessible=!!data.github_accessible;
      if (!hasOrg && !hasSelected && !hasManual && !hasAccessible) {
        errors.push(['github-org-select','Choose an organisation, load/select accessible repos, paste a manual list, or enable “Analyse every accessible repository”.']);
      }
    } else {
      if (!data.gitlab_token_name.trim()) errors.push(['gitlab_token_name','Enter the GitLab token key.']);
      if (!data.gitlab_group.trim()) errors.push(['gitlab-group-select','Choose a GitLab group.']);
    }
  }
  if (data.llm_enabled && !document.querySelector('[name=openai_key]').value.trim()) errors.push(['openai_key','Enter an OpenAI API key to enable LLM analysis.']);
  if (!errors.length) return true;
  showFormError(errors[0][1]);
  errors.forEach(([name])=>markInvalid(name));
  document.querySelector('[name="'+errors[0][0]+'"],#'+errors[0][0])?.scrollIntoView({behavior:'smooth', block:'center'});
  return false;
}
function readFormSettings(){
  const form=document.querySelector('#extract-form');
  const data=new FormData(form);
  return {
    mode:data.get('mode')||'offline',
    local_repos_dir:data.get('local_repos_dir')||'',
    hosted_platform:data.get('hosted_platform')||'github',
    tokens_file:data.get('tokens_file')||'tokens',
    github_org:data.get('github_org')||'',
    github_token_name:data.get('github_token_name')||'data-lh2-github-token',
    gitlab_group:data.get('gitlab_group')||'',
    gitlab_token_name:data.get('gitlab_token_name')||'gitlab_token',
    workers:data.get('workers')||'4',
    llm_enabled:!!data.get('llm_enabled'),
    github_accessible:!!data.get('github_accessible'),
    selected_repos:getSelectedRepos().join('\\n'),
    manual_repos:getManualRepos().join('\\n'),
  };
}
function restoreFormSettings(settings){
  if (!settings) return;
  const modeInput=document.querySelector('input[name=mode][value="'+(settings.mode||'offline')+'"]');
  if (modeInput) modeInput.checked=true;
  const setValue=(name,value)=>{ const el=document.querySelector('[name="'+name+'"]'); if (el && value!=null) el.value=value; };
  setValue('local_repos_dir', settings.local_repos_dir||'');
  setValue('hosted_platform', settings.hosted_platform||'github');
  setValue('tokens_file', settings.tokens_file||'tokens');
  setValue('github_org', settings.github_org||'');
  setValue('github_token_name', settings.github_token_name||'data-lh2-github-token');
  setValue('gitlab_group', settings.gitlab_group||'');
  setValue('gitlab_token_name', settings.gitlab_token_name||'gitlab_token');
  setValue('workers', settings.workers||'4');
  setValue('manual_repos', settings.manual_repos||'');
  document.querySelector('#llm-enabled').checked=!!settings.llm_enabled;
  const accessible=document.querySelector('#github-accessible');
  if (accessible) accessible.checked=!!settings.github_accessible;
  savedRepoChecks=new Set((settings.selected_repos||'').split(/\\n+/).filter(Boolean));
  choose();
}
function persistFormSettings(settings){ try { localStorage.setItem(FORM_STORAGE_KEY, JSON.stringify(settings)); } catch (_) {} }
function loadStoredFormSettings(){ try { const raw=localStorage.getItem(FORM_STORAGE_KEY); return raw ? JSON.parse(raw) : null; } catch (_) { return null; } }
function showFormError(message){ const el=document.querySelector('#form-error'); if (!message) { el.textContent=''; el.classList.add('hidden'); return; } el.textContent=message; el.classList.remove('hidden'); }
document.querySelectorAll('input[name=mode]').forEach(e=>e.addEventListener('change',choose));
document.querySelector('#hosted-platform').addEventListener('change',choosePlatform);
document.querySelector('#llm-enabled').addEventListener('change',chooseLlm);
document.querySelector('#load-local-repos').addEventListener('click', loadLocalRepos);
document.querySelector('#load-github-orgs').addEventListener('click', loadHostedOrgs);
document.querySelector('#load-github-accessible').addEventListener('click', loadAccessibleGithubRepos);
document.querySelector('#load-gitlab-groups').addEventListener('click', loadHostedOrgs);
document.querySelector('#github-org-select').addEventListener('change', loadHostedRepos);
document.querySelector('#gitlab-group-select').addEventListener('change', loadHostedRepos);
document.querySelector('#select-all-repos').addEventListener('click', ()=>document.querySelectorAll('input[name="selected_repos"]').forEach(el=>el.checked=true));
document.querySelector('#clear-repos').addEventListener('click', ()=>document.querySelectorAll('input[name="selected_repos"]').forEach(el=>el.checked=false));
document.querySelector('#extract-form').addEventListener('input', ()=>{ persistFormSettings(readFormSettings()); clearInvalid(); });
document.querySelector('#extract-form').addEventListener('change', ()=>persistFormSettings(readFormSettings()));
document.querySelector('#extract-form').addEventListener('submit', async (event)=>{
  event.preventDefault();
  showFormError('');
  if (!validateForm()) return;
  const form=event.target;
  const body=new URLSearchParams(new FormData(form));
  const response=await fetch('/start', {method:'POST', headers:{'Content-Type':'application/x-www-form-urlencoded'}, body});
  const text=await response.text();
  if (!response.ok) { showFormError(text || 'Unable to start analysis.'); return; }
  persistFormSettings(readFormSettings());
  window.loadedSummary=false;
});
async function refresh(){
  const data=await fetch('/status').then(r=>r.json());
  if (data.form_settings && !window.formRestoredFromServer) {
    restoreFormSettings(data.form_settings);
    persistFormSettings(data.form_settings);
    window.formRestoredFromServer=true;
  }
  const phaseLabels={idle:'Ready', running:'Analysis running…', completed:'Completed successfully', failed:'Finished with errors'};
  const label=data.running ? phaseLabels.running : phaseLabels[data.phase] || (data.returncode === 0 ? 'Completed successfully' : data.returncode === null ? 'Ready' : 'Finished with errors');
  const status=document.querySelector('#status'); status.textContent=label; status.className='status '+(data.phase==='completed' || data.returncode===0?'good':data.phase==='running' || data.returncode===null?'':'bad');
  const meta=document.querySelector('#run-meta');
  const metaParts=[];
  if (data.started_at) metaParts.push('Started: '+new Date(data.started_at).toLocaleString());
  if (data.finished_at) metaParts.push('Finished: '+new Date(data.finished_at).toLocaleString());
  if (data.repos_ok!=null) metaParts.push('Repos OK: '+data.repos_ok);
  if (data.repos_failed!=null && data.repos_failed>0) metaParts.push('Repos failed: '+data.repos_failed);
  if (metaParts.length) { meta.textContent=metaParts.join(' · '); meta.classList.remove('hidden'); }
  else { meta.textContent=''; meta.classList.add('hidden'); }
  const progress=data.progress||{};
  const progressWrap=document.querySelector('#progress-wrap');
  const progressBar=document.querySelector('#progress-bar');
  const progressLabel=document.querySelector('#progress-label');
  const showProgress=data.running && progress.total>0;
  progressWrap.classList.toggle('hidden', !showProgress);
  progressLabel.classList.toggle('hidden', !showProgress);
  if (showProgress) {
    progressBar.style.width=(progress.percent||0)+'%';
    const current=progress.current ? ' · current: '+progress.current : '';
    progressLabel.textContent=(progress.done||0)+' / '+progress.total+' repositories'+current;
  }
  document.querySelector('#log').textContent=(data.log||[]).join('\\n') || 'No analysis has started.';
  document.querySelector('#start').disabled=data.running;
  showFormError(data.last_error||'');
  const results=document.querySelector('#results');
  const showResults=data.phase==='completed' && data.summary_path;
  results.classList.toggle('hidden', !showResults);
  const paths=document.querySelector('#artifact-paths');
  if (showResults) {
    const parts=[];
    if (data.xlsx_path) parts.push('<strong>Summary (Excel):</strong> <code>'+data.xlsx_path+'</code>');
    else if (data.summary_path) parts.push('<strong>Summary (CSV):</strong> <code>'+data.summary_path+'</code>');
    if (data.zip_path) parts.push('<strong>Archive zip:</strong> <code>'+data.zip_path+'</code>');
    if (data.host_output_hint) parts.push('<strong>Host folder:</strong> <code>'+data.host_output_hint+'</code>');
    paths.innerHTML=parts.join('<br>');
    paths.classList.toggle('hidden', parts.length===0);
    const token='csrf_token=__CSRF_TOKEN__';
    const summaryLink=document.querySelector('#download-summary');
    const zipLink=document.querySelector('#download-zip');
    summaryLink.href='/download/summary?'+token;
    summaryLink.classList.remove('disabled');
    if (data.zip_path) {
      zipLink.href='/download/zip?'+token;
      zipLink.classList.remove('disabled');
    } else {
      zipLink.href='#';
      zipLink.classList.add('disabled');
    }
  } else {
    paths.classList.add('hidden');
  }
  if (data.summary_path && data.phase==='completed' && window.loadedSummaryFor!==data.summary_path) {
    window.loadedSummaryFor=data.summary_path;
    const csv=await fetch('/summary').then(r=>r.json());
    const table=document.createElement('table');
    table.style.cssText='border-collapse:collapse;width:100%;font-size:12px;margin-top:12px';
    csv.rows.forEach((row,index)=>{ const tr=document.createElement('tr'); row.forEach(cell=>{ const td=document.createElement(index?'td':'th'); td.textContent=cell; td.style.cssText='padding:7px;border:1px solid #dce3ef;text-align:left;vertical-align:top'; tr.appendChild(td); }); table.appendChild(tr); });
    const target=document.querySelector('#csv-preview'); target.textContent=''; target.appendChild(table);
  }
  if (!data.summary_path || data.phase!=='completed') window.loadedSummaryFor=null;
}
document.querySelector('#open-folder').addEventListener('click', async ()=> {
  const response=await fetch('/open-output', {method:'POST', headers:{'Content-Type':'application/x-www-form-urlencoded'}, body:'csrf_token=__CSRF_TOKEN__'});
  let payload;
  try { payload=await response.json(); } catch (_) { payload={message:await response.text()}; }
  if (!response.ok) { alert(payload.message || 'Unable to open output folder.'); return; }
  if (!payload.opened) alert(payload.message || ('Folder path: '+payload.path));
});
restoreFormSettings(loadStoredFormSettings());
choose();
setInterval(refresh,1200); refresh();
</script></main></body></html>""".replace("__CSRF_TOKEN__", CSRF_TOKEN).replace(
        "__DOCKER_NOTICE__", docker_notice
    ).replace("__DEFAULT_LOCAL_REPOS_DIR__", escape(default_local)).replace(
        "__DEFAULT_TOKENS_FILE__", escape(default_tokens)
    ).replace("__LOCAL_PLACEHOLDER__", escape(local_placeholder))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, _format: str, *_args: Any) -> None:
        """Avoid noisy HTTP request logging in the extractor terminal."""

    def respond(self, status: int, content_type: str, body: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def respond_file(
        self,
        file_path: Path,
        download_name: str,
        content_type: str,
        *,
        attachment: bool = True,
    ) -> None:
        with file_path.open("rb") as handle:
            payload = handle.read()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        disposition = "attachment" if attachment else "inline"
        self.send_header("Content-Disposition", f'{disposition}; filename="{download_name}"')
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        query = parse_qs(urlparse(self.path).query)
        if path == "/logo.svg":
            if not LOGO_PATH.is_file():
                self.respond(HTTPStatus.NOT_FOUND, "text/plain", "Logo unavailable.")
                return
            self.respond_file(LOGO_PATH, "LH2-DataLabs.svg", "image/svg+xml", attachment=False)
            return
        if path == "/status":
            with LOCK:
                safe_state = {
                    key: value
                    for key, value in STATE.items()
                    if key != "command"
                }
                safe_state["progress"] = compute_progress(safe_state.get("log") or [])
                if is_docker_mode():
                    safe_state["host_output_hint"] = os.environ.get(
                        "HOST_OUTPUT_HINT", "./outputs/raw-extracts"
                    )
            self.respond(HTTPStatus.OK, "application/json", json.dumps(safe_state))
            return
        if path == "/download/summary":
            if not secrets.compare_digest(query.get("csrf_token", [""])[0], CSRF_TOKEN):
                self.respond(HTTPStatus.FORBIDDEN, "text/plain", "Invalid request token.")
                return
            with LOCK:
                summary_value = STATE.get("xlsx_path") or STATE.get("summary_path")
            if not summary_value:
                self.respond(HTTPStatus.NOT_FOUND, "text/plain", "No completed summary is available.")
                return
            summary_path = Path(summary_value)
            if not summary_path.is_file() or not is_under_archive(summary_path):
                self.respond(HTTPStatus.NOT_FOUND, "text/plain", "Summary file is unavailable.")
                return
            if summary_path.suffix.lower() == ".xlsx":
                self.respond_file(summary_path, "summary.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            else:
                self.respond_file(summary_path, "summary.csv", "text/csv; charset=utf-8")
            return
        if path == "/download/zip":
            if not secrets.compare_digest(query.get("csrf_token", [""])[0], CSRF_TOKEN):
                self.respond(HTTPStatus.FORBIDDEN, "text/plain", "Invalid request token.")
                return
            with LOCK:
                zip_value = STATE.get("zip_path")
            if not zip_value:
                self.respond(HTTPStatus.NOT_FOUND, "text/plain", "No completed archive zip is available.")
                return
            zip_path = Path(zip_value)
            if not zip_path.is_file() or not is_under_archive(zip_path):
                self.respond(HTTPStatus.NOT_FOUND, "text/plain", "Archive zip is unavailable.")
                return
            self.respond_file(zip_path, zip_path.name, "application/zip")
            return
        if path == "/summary":
            with LOCK:
                summary_value = STATE.get("summary_path")
            if not summary_value:
                self.respond(
                    HTTPStatus.NOT_FOUND,
                    "text/plain",
                    "No completed summary is available.",
                )
                return
            summary_path = Path(summary_value)
            try:
                with summary_path.open(newline="", encoding="utf-8") as handle:
                    rows = list(reader(handle))
            except OSError:
                self.respond(
                    HTTPStatus.NOT_FOUND,
                    "text/plain",
                    "Summary file is unavailable.",
                )
                return
            self.respond(
                HTTPStatus.OK,
                "application/json",
                json.dumps({"rows": rows[:501], "truncated": len(rows) > 501}),
            )
            return
        self.respond(HTTPStatus.OK, "text/html", page())

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path not in {
            "/start",
            "/open-output",
            "/discover/local",
            "/discover/orgs",
            "/discover/repos",
            "/discover/accessible-repos",
        }:
            self.respond(HTTPStatus.NOT_FOUND, "text/plain", "Not found")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.respond(HTTPStatus.BAD_REQUEST, "text/plain", "Invalid form submission.")
            return
        if length <= 0 or length > 100_000:
            self.respond(HTTPStatus.BAD_REQUEST, "text/plain", "Invalid form submission.")
            return
        fields = parse_qs(self.rfile.read(length).decode("utf-8"))
        submitted_token = fields.get("csrf_token", [""])[0]
        if not secrets.compare_digest(submitted_token, CSRF_TOKEN):
            self.respond(HTTPStatus.FORBIDDEN, "text/plain", "Invalid request token.")
            return
        if path == "/open-output":
            with LOCK:
                output_value = STATE.get("run_dir") or STATE.get("output_dir")
            if not output_value:
                self.respond(
                    HTTPStatus.NOT_FOUND,
                    "application/json",
                    json.dumps({"opened": False, "message": "No completed output folder is available."}),
                )
                return
            output_dir = Path(output_value)
            if not output_dir.is_dir() or not is_under_archive(output_dir):
                self.respond(
                    HTTPStatus.NOT_FOUND,
                    "application/json",
                    json.dumps({"opened": False, "message": "Output folder is unavailable."}),
                )
                return
            result = open_output_folder(output_dir)
            self.respond(HTTPStatus.OK, "application/json", json.dumps(result))
            return
        if path == "/discover/local":
            local_dir = fields.get("local_repos_dir", [""])[0].strip()
            if not local_dir:
                self.respond(
                    HTTPStatus.BAD_REQUEST,
                    "application/json",
                    json.dumps({"error": "Choose the folder holding local clones."}),
                )
                return
            targets = discover_local_repositories(Path(local_dir).resolve())
            items = [{"id": target.full_name, "name": target.full_name} for target in targets]
            self.respond(HTTPStatus.OK, "application/json", json.dumps({"items": items}))
            return
        if path == "/discover/orgs":
            platform = fields.get("hosted_platform", ["github"])[0]
            try:
                token = read_token_from_fields(fields, platform)
            except ValueError as exc:
                self.respond(
                    HTTPStatus.BAD_REQUEST,
                    "application/json",
                    json.dumps({"error": str(exc)}),
                )
                return
            try:
                if platform == "github":
                    items = list_github_orgs_for_token(token)
                elif platform == "gitlab":
                    items = list_gitlab_groups_for_token(token)
                else:
                    self.respond(
                        HTTPStatus.BAD_REQUEST,
                        "application/json",
                        json.dumps({"error": "Unknown hosted platform."}),
                    )
                    return
            except Exception as exc:
                self.respond(
                    HTTPStatus.BAD_REQUEST,
                    "application/json",
                    json.dumps({"error": str(exc)[:500]}),
                )
                return
            self.respond(HTTPStatus.OK, "application/json", json.dumps({"items": items}))
            return
        if path == "/discover/repos":
            platform = fields.get("hosted_platform", ["github"])[0]
            try:
                token = read_token_from_fields(fields, platform)
            except ValueError as exc:
                self.respond(
                    HTTPStatus.BAD_REQUEST,
                    "application/json",
                    json.dumps({"error": str(exc)}),
                )
                return
            try:
                if platform == "github":
                    org = fields.get("github_org", [""])[0].strip()
                    if not org:
                        self.respond(
                            HTTPStatus.BAD_REQUEST,
                            "application/json",
                            json.dumps({"error": "Choose a GitHub organisation."}),
                        )
                        return
                    items = list_github_repos_for_org(token, org)
                elif platform == "gitlab":
                    group = fields.get("gitlab_group", [""])[0].strip()
                    if not group:
                        self.respond(
                            HTTPStatus.BAD_REQUEST,
                            "application/json",
                            json.dumps({"error": "Choose a GitLab group."}),
                        )
                        return
                    items = list_gitlab_projects_for_group(token, group)
                else:
                    self.respond(
                        HTTPStatus.BAD_REQUEST,
                        "application/json",
                        json.dumps({"error": "Unknown hosted platform."}),
                    )
                    return
            except Exception as exc:
                self.respond(
                    HTTPStatus.BAD_REQUEST,
                    "application/json",
                    json.dumps({"error": str(exc)[:500]}),
                )
                return
            self.respond(HTTPStatus.OK, "application/json", json.dumps({"items": items}))
            return
        if path == "/discover/accessible-repos":
            platform = fields.get("hosted_platform", ["github"])[0]
            if platform != "github":
                self.respond(
                    HTTPStatus.BAD_REQUEST,
                    "application/json",
                    json.dumps({"error": "Accessible repository discovery is only available for GitHub."}),
                )
                return
            try:
                token = read_token_from_fields(fields, platform)
            except ValueError as exc:
                self.respond(
                    HTTPStatus.BAD_REQUEST,
                    "application/json",
                    json.dumps({"error": str(exc)}),
                )
                return
            try:
                items = list_github_accessible_repos(token)
            except Exception as exc:
                self.respond(
                    HTTPStatus.BAD_REQUEST,
                    "application/json",
                    json.dumps({"error": str(exc)[:500]}),
                )
                return
            self.respond(HTTPStatus.OK, "application/json", json.dumps({"items": items}))
            return
        mode = fields.get("mode", ["offline"])[0]
        form_settings = extract_form_settings(fields)
        selected_repos = extract_selected_repos(fields)
        manual_repos = parse_repo_selectors(fields.get("manual_repos", [""])[0])
        selected_repos = merge_repo_selectors(selected_repos, manual_repos)
        workers = fields.get("workers", ["4"])[0].strip()
        try:
            workers_number = int(workers)
        except ValueError:
            self.respond(HTTPStatus.BAD_REQUEST, "text/plain", "Workers must be a whole number.")
            return
        if not 1 <= workers_number <= 20:
            self.respond(HTTPStatus.BAD_REQUEST, "text/plain", "Workers must be between 1 and 20.")
            return
        command = [
            sys.executable,
            str(EXTRACTOR),
            "--output-dir",
            str(DEFAULT_OUTPUT),
            "--workers",
            str(workers_number),
        ]

        if mode == "offline":
            local_dir = fields.get("local_repos_dir", [""])[0].strip()
            if not local_dir:
                self.respond(HTTPStatus.BAD_REQUEST, "text/plain", "Choose the folder holding local clones.")
                return
            command.extend(["--offline", "--local-repos-dir", local_dir])
            for repo_name in selected_repos:
                command.extend(["--local-repo", repo_name])
        elif mode == "hosted":
            tokens_file = fields.get("tokens_file", [""])[0].strip()
            if not tokens_file:
                self.respond(HTTPStatus.BAD_REQUEST, "text/plain", "Enter the token file path.")
                return
            command.extend(["--tokens-file", tokens_file])
            platform = fields.get("hosted_platform", ["github"])[0]
            if platform == "github":
                org = fields.get("github_org", [""])[0].strip()
                token_name = fields.get("github_token_name", [""])[0].strip()
                github_accessible = fields.get("github_accessible", [""])[0] == "on"
                if not token_name:
                    self.respond(
                        HTTPStatus.BAD_REQUEST,
                        "text/plain",
                        "Enter the GitHub token key.",
                    )
                    return
                if selected_repos:
                    for repo_name in selected_repos:
                        command.extend(["--github-repo", repo_name])
                elif github_accessible:
                    command.append("--github-accessible")
                elif org:
                    command.extend(["--github-org", org])
                else:
                    self.respond(
                        HTTPStatus.BAD_REQUEST,
                        "text/plain",
                        "Choose a GitHub organisation, select/paste repositories, "
                        "or enable “Analyse every accessible repository”.",
                    )
                    return
                command.extend(["--github-token-name", token_name])
            elif platform == "gitlab":
                group = fields.get("gitlab_group", [""])[0].strip()
                token_name = fields.get("gitlab_token_name", [""])[0].strip()
                if not token_name:
                    self.respond(
                        HTTPStatus.BAD_REQUEST,
                        "text/plain",
                        "Enter the GitLab token key.",
                    )
                    return
                if not group:
                    self.respond(
                        HTTPStatus.BAD_REQUEST,
                        "text/plain",
                        "Choose a GitLab group.",
                    )
                    return
                command.extend(["--gitlab-group", group])
                for project in selected_repos:
                    command.extend(["--gitlab-repo", project])
                command.extend(["--gitlab-token-name", token_name])
            else:
                self.respond(HTTPStatus.BAD_REQUEST, "text/plain", "Unknown hosted platform.")
                return
        else:
            self.respond(HTTPStatus.BAD_REQUEST, "text/plain", "Unknown mode.")
            return

        env_overrides: dict[str, str] = {}
        if fields.get("llm_enabled", [""])[0] == "on":
            openai_key = fields.get("openai_key", [""])[0].strip()
            if not openai_key:
                self.respond(
                    HTTPStatus.BAD_REQUEST,
                    "text/plain",
                    "Enter an OpenAI API key to enable LLM analysis.",
                )
                return
            command.append("--llm")
            # Keep the key out of command arguments, logs, and output archives.
            env_overrides["OPENAI_API_KEY"] = openai_key

        with LOCK:
            if STATE["running"]:
                self.respond(HTTPStatus.CONFLICT, "text/plain", "An extraction is already running.")
                return
        set_state(phase="running", running=True, form_settings=form_settings, last_error=None)
        threading.Thread(
            target=run_extraction,
            args=(command, env_overrides),
            daemon=True,
        ).start()
        self.respond(HTTPStatus.OK, "application/json", json.dumps({"ok": True}))


class LocalHTTPServer(ThreadingHTTPServer):
    """Permit a quick restart after the local UI is stopped."""

    allow_reuse_address = True


def serve(host: str = "127.0.0.1", port: int = 8766) -> None:
    """Host the local UI and open it in the default browser."""
    docker_mode = is_docker_mode()
    allowed_hosts = {"127.0.0.1", "localhost", "::1"}
    if docker_mode:
        allowed_hosts.add("0.0.0.0")
    if host not in allowed_hosts:
        raise ValueError("The UI may only bind to localhost for credential safety.")
    load_persisted_state()
    server = LocalHTTPServer((host, port), Handler)
    if host == "0.0.0.0":
        url = f"http://localhost:{port}"
    else:
        url = f"http://{host}:{port}"
    print(f"Repository Evidence Extractor UI: {url}")
    if docker_mode:
        print("Docker mode: open the URL above in your browser on this computer.")
    elif host in {"127.0.0.1", "localhost"}:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nUI stopped.")
    finally:
        server.server_close()
