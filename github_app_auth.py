#!/usr/bin/env python3
"""GitHub App authentication helpers (JWT + installation access tokens)."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import jwt

DEFAULT_GITHUB_APP_ID = "4284468"
DEFAULT_GITHUB_APP_PEM = Path(__file__).resolve().parent / (
    "data-labs-codebase-analysis.2026-07-12.private-key.pem"
)


def github_api_base(host: str = "github.com") -> str:
    return "https://api.github.com" if host == "github.com" else f"https://{host}/api/v3"


def load_private_key(pem_path: Path) -> str:
    if not pem_path.exists():
        raise FileNotFoundError(f"GitHub App private key not found: {pem_path}")
    return pem_path.read_text(encoding="utf-8")


def make_app_jwt(app_id: str | int, pem_path: Path, *, lifetime_seconds: int = 540) -> str:
    """Sign a short-lived JWT to authenticate as the GitHub App."""
    now = datetime.now(timezone.utc)
    payload = {
        "iat": int((now - timedelta(seconds=60)).timestamp()),
        "exp": int((now + timedelta(seconds=lifetime_seconds)).timestamp()),
        "iss": str(app_id),
    }
    return jwt.encode(payload, load_private_key(pem_path), algorithm="RS256")


def _request_json(
    method: str,
    url: str,
    *,
    token: str,
    token_type: str = "Bearer",
    body: dict[str, Any] | None = None,
) -> tuple[Any, dict[str, str]]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"{token_type} {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "lh2-github-app-auth",
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode("utf-8")
            hdrs = {k: v for k, v in resp.headers.items()}
            return (json.loads(raw) if raw else None), hdrs
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} for {url}: {detail[:800]}") from exc


def list_installations(
    app_id: str | int,
    pem_path: Path,
    *,
    host: str = "github.com",
) -> list[dict[str, Any]]:
    """
    Fetch every installation of this GitHub App.

    Returns a list of dicts with at least:
      installation_id, account_login, account_type, repository_selection,
      html_url, suspended_at, permissions, raw
    """
    api = github_api_base(host)
    app_jwt = make_app_jwt(app_id, pem_path)
    installations: list[dict[str, Any]] = []
    page = 1
    while True:
        url = f"{api}/app/installations?per_page=100&page={page}"
        data, headers = _request_json("GET", url, token=app_jwt)
        if not isinstance(data, list):
            break
        for item in data:
            account = item.get("account") or {}
            installations.append(
                {
                    "installation_id": item.get("id"),
                    "account_login": account.get("login"),
                    "account_type": account.get("type"),
                    "account_id": account.get("id"),
                    "repository_selection": item.get("repository_selection"),
                    "html_url": item.get("html_url"),
                    "suspended_at": item.get("suspended_at"),
                    "permissions": item.get("permissions") or {},
                    "created_at": item.get("created_at"),
                    "updated_at": item.get("updated_at"),
                    "raw": item,
                }
            )
        if 'rel="next"' not in headers.get("Link", ""):
            break
        page += 1
    return installations


def find_installation_id(
    app_id: str | int,
    pem_path: Path,
    account_login: str,
    *,
    host: str = "github.com",
) -> int:
    """Resolve installation_id for an org/user login (case-insensitive)."""
    wanted = account_login.strip().lower()
    for inst in list_installations(app_id, pem_path, host=host):
        login = (inst.get("account_login") or "").lower()
        if login == wanted:
            iid = inst.get("installation_id")
            if iid is None:
                raise RuntimeError(f"Installation for {account_login!r} has no id")
            return int(iid)
    raise RuntimeError(
        f"No GitHub App installation found for account {account_login!r}. "
        "Ask them to install the app, then re-run --list-installations."
    )


def create_installation_token(
    app_id: str | int,
    pem_path: Path,
    installation_id: int | str,
    *,
    host: str = "github.com",
) -> dict[str, Any]:
    """Mint a short-lived installation access token (~1 hour)."""
    api = github_api_base(host)
    app_jwt = make_app_jwt(app_id, pem_path)
    url = f"{api}/app/installations/{installation_id}/access_tokens"
    data, _ = _request_json("POST", url, token=app_jwt, body={})
    if not isinstance(data, dict) or not data.get("token"):
        raise RuntimeError(f"Failed to create installation token: {data!r}"[:500])
    return data


class InstallationTokenProvider:
    """Thread-safe installation token that refreshes before expiry."""

    def __init__(
        self,
        app_id: str | int,
        pem_path: Path,
        installation_id: int | str,
        *,
        host: str = "github.com",
        refresh_skew_seconds: int = 120,
    ) -> None:
        self.app_id = app_id
        self.pem_path = pem_path
        self.installation_id = installation_id
        self.host = host
        self.refresh_skew_seconds = refresh_skew_seconds
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires_at: float = 0.0

    def get(self) -> str:
        with self._lock:
            now = time.time()
            if self._token and now < (self._expires_at - self.refresh_skew_seconds):
                return self._token
            payload = create_installation_token(
                self.app_id,
                self.pem_path,
                self.installation_id,
                host=self.host,
            )
            self._token = str(payload["token"])
            expires_at = payload.get("expires_at")
            if expires_at:
                try:
                    self._expires_at = datetime.fromisoformat(
                        expires_at.replace("Z", "+00:00")
                    ).timestamp()
                except ValueError:
                    self._expires_at = now + 3600
            else:
                self._expires_at = now + 3600
            return self._token
