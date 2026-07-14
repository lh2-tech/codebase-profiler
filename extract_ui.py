#!/usr/bin/env python3
"""Local, dependency-free browser UI for extract_org_raw_data.py."""

from __future__ import annotations

import json
import os
import secrets
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
CSRF_TOKEN = secrets.token_urlsafe(32)

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


def extract_form_settings(fields: dict[str, list[str]]) -> dict[str, Any]:
    llm_enabled = fields.get("llm_enabled", [""])[0] == "on"
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
        add_log("Starting extraction…")
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
            "your computer. The Open archive folder button opens inside the container only."
            "</p>"
        )
    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Repository Evidence Extractor</title>
<style>
  :root { color-scheme: light; font-family: Inter, system-ui, sans-serif; color:#172033; background:#f4f7fb; }
  body { margin:0; } main { max-width:900px; margin:0 auto; padding:34px 22px 54px; }
  h1 { margin:0; font-size:28px; } .lead { color:#536078; margin:8px 0 28px; }
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
  .paths { font-size:13px; color:#536078; margin-top:10px; line-height:1.5; }
  .paths code { font-size:12px; word-break:break-all; }
  .form-error { color:#b91c1c; font-weight:600; font-size:14px; margin-top:10px; }
  .run-meta { font-size:13px; color:#536078; margin-top:6px; }
</style></head><body><main>
<h1>Repository Evidence Extractor</h1>
<p class="lead">Creates a metadata archive for later analysis. Source code is analysed locally and is not included in the output zip; commit, pull-request, and issue metadata may be retained.</p>
__DOCKER_NOTICE__
<form id="extract-form" method="post" action="/start">
<input type="hidden" name="csrf_token" value="__CSRF_TOKEN__">
<div class="card"><strong>Where are the repositories?</strong>
<div class="choices">
  <label class="choice"><input type="radio" name="mode" value="offline" checked> <strong>Already cloned here</strong><span class="small">Runs entirely offline. No internet connection is needed.</span></label>
  <label class="choice"><input type="radio" name="mode" value="hosted"> <strong>Hosted platform</strong><span class="small">Connect to a GitHub or GitLab organisation with a token file.</span></label>
</div></div>
<div class="card">
  <div id="offline-fields"><label class="field">Folder holding full local clones</label><input name="local_repos_dir" value="__DEFAULT_LOCAL_REPOS_DIR__" placeholder="__LOCAL_PLACEHOLDER__"><p class="notice"><strong>How to prepare this folder:</strong> use a normal full clone for every repository, for example <code>git clone https://github.com/OWNER/REPO.git</code>. Do not use <code>--depth</code>, because the extractor needs the complete commit history. Put one or more cloned repositories inside this folder, then select the folder above.</p></div>
  <div id="hosted-fields" class="hidden">
    <label class="field">Platform</label><select id="hosted-platform" name="hosted_platform"><option value="github">GitHub</option><option value="gitlab">GitLab</option></select>
    <label class="field">Path to token file</label><input name="tokens_file" value="__DEFAULT_TOKENS_FILE__" placeholder="/path/to/tokens">
    <div id="github-fields"><label class="field">GitHub organisation name</label><input name="github_org" placeholder="CustomerOrg"><label class="field">GitHub token key</label><input name="github_token_name" value="data-lh2-github-token" placeholder="Key in the token file"></div>
    <div id="gitlab-fields" class="hidden"><label class="field">GitLab group path</label><input name="gitlab_group" placeholder="customer-group or customer-group/subgroup"><label class="field">GitLab token key</label><input name="gitlab_token_name" value="gitlab_token" placeholder="Key in the token file"></div>
  </div>
  <label class="field">Parallel workers</label><input name="workers" type="number" value="4" min="1" max="20">
  <label class="choice" style="margin-top:18px;display:flex;align-items:center"><input id="llm-enabled" type="checkbox" name="llm_enabled"><strong>Enable LLM analysis</strong><span class="small">Adds codebase description, industry/domain, vibe-code signals, and repository type.</span></label>
  <div id="llm-fields" class="hidden"><label class="field">OpenAI API key</label><input name="openai_key" type="password" autocomplete="off" placeholder="sk-..."><p class="notice"><strong>Data sent to OpenAI:</strong> repository name and aggregate metrics, up to 250 file paths, up to 4 source-code excerpts (maximum 1,500 characters each), and one README excerpt (maximum 4,000 characters). These excerpts and the API key are not stored in the output archive.</p><p id="offline-llm-warning" class="warning hidden">LLM mode requires an internet connection in offline mode and sends the data described above to OpenAI.</p></div>
  <button id="start">Create evidence archive</button>
  <p id="form-error" class="form-error hidden"></p>
  <p class="notice">The browser interface only listens on this computer. Keep this page open while the extraction runs.</p>
</div></form>
<div class="card"><span id="status" class="status">Ready</span><p id="run-meta" class="run-meta hidden"></p><pre id="log">No extraction has started.</pre></div>
<div id="results" class="card hidden"><strong>Summary CSV</strong>
<div class="actions">
  <button id="open-folder" type="button" class="secondary">Open archive folder</button>
</div>
<p id="artifact-paths" class="paths hidden"></p>
<div class="csv-scroll"><div id="csv-preview" class="notice">Loading summary…</div></div></div>
<script>
const FORM_STORAGE_KEY='extract-ui-form-v1';
const forms = {offline:document.querySelector('#offline-fields'), hosted:document.querySelector('#hosted-fields')};
function choose(){ const mode=document.querySelector('input[name=mode]:checked').value; Object.entries(forms).forEach(([k,e])=>e.classList.toggle('hidden', k!==mode)); choosePlatform(); chooseLlm(); }
function choosePlatform(){ const platform=document.querySelector('#hosted-platform').value; document.querySelector('#github-fields').classList.toggle('hidden', platform!=='github'); document.querySelector('#gitlab-fields').classList.toggle('hidden', platform!=='gitlab'); }
function chooseLlm(){ const enabled=document.querySelector('#llm-enabled').checked; const offline=document.querySelector('input[name=mode]:checked').value==='offline'; document.querySelector('#llm-fields').classList.toggle('hidden', !enabled); document.querySelector('#offline-llm-warning').classList.toggle('hidden', !(enabled && offline)); }
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
  document.querySelector('#llm-enabled').checked=!!settings.llm_enabled;
  choose();
}
function persistFormSettings(settings){
  try { localStorage.setItem(FORM_STORAGE_KEY, JSON.stringify(settings)); } catch (_) {}
}
function loadStoredFormSettings(){
  try {
    const raw=localStorage.getItem(FORM_STORAGE_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch (_) { return null; }
}
function showFormError(message){
  const el=document.querySelector('#form-error');
  if (!message) { el.textContent=''; el.classList.add('hidden'); return; }
  el.textContent=message; el.classList.remove('hidden');
}
document.querySelectorAll('input[name=mode]').forEach(e=>e.addEventListener('change',choose));
document.querySelector('#hosted-platform').addEventListener('change',choosePlatform);
document.querySelector('#llm-enabled').addEventListener('change',chooseLlm);
document.querySelector('#extract-form').addEventListener('input', ()=>persistFormSettings(readFormSettings()));
document.querySelector('#extract-form').addEventListener('change', ()=>persistFormSettings(readFormSettings()));
document.querySelector('#extract-form').addEventListener('submit', async (event)=>{
  event.preventDefault();
  showFormError('');
  const form=event.target;
  const body=new URLSearchParams(new FormData(form));
  const response=await fetch('/start', {method:'POST', headers:{'Content-Type':'application/x-www-form-urlencoded'}, body});
  const text=await response.text();
  if (!response.ok) { showFormError(text || 'Unable to start extraction.'); return; }
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
  const phaseLabels={idle:'Ready', running:'Extraction running…', completed:'Completed successfully', failed:'Finished with errors'};
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
  document.querySelector('#log').textContent=(data.log||[]).join('\\n') || 'No extraction has started.';
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
    paths.innerHTML=parts.join('<br>');
    paths.classList.toggle('hidden', parts.length===0);
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
  if (!response.ok) alert(await response.text());
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

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/status":
            with LOCK:
                safe_state = {
                    key: value
                    for key, value in STATE.items()
                    if key != "command"
                }
            self.respond(HTTPStatus.OK, "application/json", json.dumps(safe_state))
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
        if path not in {"/start", "/open-output"}:
            self.respond(HTTPStatus.NOT_FOUND, "text/plain", "Not found")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.respond(HTTPStatus.BAD_REQUEST, "text/plain", "Invalid form submission.")
            return
        if length <= 0 or length > 20_000:
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
                    "text/plain",
                    "No completed output folder is available.",
                )
                return
            output_dir = Path(output_value)
            archive_root = DEFAULT_OUTPUT.resolve()
            resolved_output = output_dir.resolve()
            if not output_dir.is_dir() or (
                resolved_output != archive_root and archive_root not in resolved_output.parents
            ):
                self.respond(
                    HTTPStatus.NOT_FOUND,
                    "text/plain",
                    "Output folder is unavailable.",
                )
                return
            if sys.platform == "darwin":
                subprocess.Popen(["open", str(output_dir)])
            elif sys.platform.startswith("win"):
                subprocess.Popen(["explorer", str(output_dir)])
            else:
                subprocess.Popen(["xdg-open", str(output_dir)])
            self.respond(HTTPStatus.OK, "text/plain", "Opened output folder.")
            return
        mode = fields.get("mode", ["offline"])[0]
        form_settings = extract_form_settings(fields)
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
                if not org or not token_name:
                    self.respond(
                        HTTPStatus.BAD_REQUEST,
                        "text/plain",
                        "Enter the GitHub organisation and token key.",
                    )
                    return
                command.extend(
                    ["--github-org", org, "--github-token-name", token_name]
                )
            elif platform == "gitlab":
                group = fields.get("gitlab_group", [""])[0].strip()
                token_name = fields.get("gitlab_token_name", [""])[0].strip()
                if not group or not token_name:
                    self.respond(
                        HTTPStatus.BAD_REQUEST,
                        "text/plain",
                        "Enter the GitLab group and token key.",
                    )
                    return
                command.extend(
                    ["--gitlab-group", group, "--gitlab-token-name", token_name]
                )
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
