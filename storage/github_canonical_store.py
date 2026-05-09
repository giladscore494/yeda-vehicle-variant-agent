from __future__ import annotations

import base64
import json
from urllib.parse import quote

import requests


def _read_streamlit_secrets() -> dict:
    try:
        import streamlit as st
    except Exception:
        return {}
    try:
        return dict(st.secrets)
    except Exception:
        return {}


def get_github_config() -> dict:
    secrets = _read_streamlit_secrets()
    token = secrets.get("GITHUB_TOKEN")
    repo = secrets.get("GITHUB_REPO")
    branch = secrets.get("GITHUB_BRANCH", "main")
    canonical_path = secrets.get("CANONICAL_RESUME_PATH", "data/canonical/resume_package_canonical.json")
    backup_path = secrets.get("CANONICAL_BACKUP_PATH", "data/canonical/resume_package_backup_previous.json")
    return {
        "token": token,
        "repo": repo,
        "branch": branch,
        "canonical_path": canonical_path,
        "backup_path": backup_path,
    }


def _contents_url(repo: str, path: str) -> str:
    return f"https://api.github.com/repos/{repo}/contents/{quote(path)}"


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _parse_json_text(raw_text: str) -> dict | None:
    try:
        payload = json.loads(raw_text)
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _get_file_payload(path: str) -> tuple[dict | None, str | None, str | None]:
    cfg = get_github_config()
    token = cfg.get("token")
    repo = cfg.get("repo")
    if not token or not repo:
        return None, None, "GitHub credentials are missing in Streamlit secrets."
    try:
        resp = requests.get(
            _contents_url(repo, path),
            headers=_headers(token),
            params={"ref": cfg.get("branch", "main")},
            timeout=30,
        )
    except Exception as exc:
        return None, None, f"Failed to fetch file from GitHub: {type(exc).__name__}"
    if resp.status_code == 404:
        return None, None, None
    if resp.status_code >= 400:
        return None, None, f"Failed to fetch file from GitHub (HTTP {resp.status_code})."
    body = resp.json() if resp.content else {}
    encoded = body.get("content")
    encoding = body.get("encoding")
    sha = body.get("sha")
    download_url = body.get("download_url")
    parse_error = None

    if isinstance(encoded, str) and encoded.strip() and str(encoding or "").lower() == "base64":
        try:
            decoded_text = base64.b64decode(encoded).decode("utf-8")
        except Exception:
            decoded_text = ""
        data = _parse_json_text(decoded_text) if decoded_text else None
        if isinstance(data, dict):
            return data, sha, None
        parse_error = "GitHub file content is not valid JSON."
    else:
        parse_error = "GitHub file payload content is missing or unusable."

    if isinstance(download_url, str) and download_url.strip():
        try:
            raw_resp = requests.get(download_url, headers=_headers(token), timeout=30)
        except Exception as exc:
            return None, sha, f"Failed to fetch GitHub file download URL: {type(exc).__name__}"
        if raw_resp.status_code >= 400:
            return None, sha, f"Failed to fetch GitHub file download URL (HTTP {raw_resp.status_code})."
        data = _parse_json_text(raw_resp.text if isinstance(raw_resp.text, str) else "")
        if isinstance(data, dict):
            return data, sha, None
        return None, sha, "GitHub file content from download_url is not valid JSON."

    return None, sha, parse_error


def fetch_file_from_github(path: str) -> dict | None:
    data, _, _ = _get_file_payload(path)
    return data if isinstance(data, dict) else None


def file_exists_on_github(path: str) -> bool:
    data, sha, err = _get_file_payload(path)
    return (data is not None or bool(sha)) and err is None


def push_file_to_github(path: str, data, commit_message: str) -> dict:
    cfg = get_github_config()
    token = cfg.get("token")
    repo = cfg.get("repo")
    branch = cfg.get("branch", "main")
    if not token or not repo:
        return {"ok": False, "error": "GitHub credentials are missing in Streamlit secrets."}
    _, sha, fetch_err = _get_file_payload(path)
    if fetch_err:
        return {"ok": False, "error": fetch_err}
    content = base64.b64encode(json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")).decode("utf-8")
    payload = {"message": commit_message, "content": content, "branch": branch}
    if sha:
        payload["sha"] = sha
    try:
        resp = requests.put(_contents_url(repo, path), headers=_headers(token), json=payload, timeout=30)
    except Exception as exc:
        return {"ok": False, "error": f"Failed to push file to GitHub: {type(exc).__name__}"}
    if resp.status_code >= 400:
        return {"ok": False, "error": f"Failed to push file to GitHub (HTTP {resp.status_code})."}
    body = resp.json() if resp.content else {}
    commit = body.get("commit") if isinstance(body, dict) else {}
    content_obj = body.get("content") if isinstance(body, dict) else {}
    return {
        "ok": True,
        "path": path,
        "sha": (content_obj or {}).get("sha"),
        "commit_sha": (commit or {}).get("sha"),
    }


def backup_canonical_on_github(current_canonical) -> dict:
    cfg = get_github_config()
    if not isinstance(current_canonical, dict):
        return {"ok": False, "error": "Current canonical package is missing or invalid."}
    return push_file_to_github(
        cfg.get("backup_path", "data/canonical/resume_package_backup_previous.json"),
        current_canonical,
        "Update canonical vehicle resume package after batch (backup)",
    )


def push_canonical_resume_package(package, previous_package=None, batch_id=None, commit_message: str | None = None) -> dict:
    cfg = get_github_config()
    backup_result = None
    if isinstance(previous_package, dict):
        backup_result = backup_canonical_on_github(previous_package)
        if not backup_result.get("ok"):
            return {"ok": False, "error": backup_result.get("error"), "backup": backup_result}
    if commit_message is None:
        commit_message = "Update canonical vehicle resume package after batch "
        if batch_id:
            commit_message = f"{commit_message}{batch_id}"
    canonical_result = push_file_to_github(
        cfg.get("canonical_path", "data/canonical/resume_package_canonical.json"),
        package,
        commit_message,
    )
    if not canonical_result.get("ok"):
        return {"ok": False, "error": canonical_result.get("error"), "backup": backup_result, "canonical": canonical_result}
    return {"ok": True, "backup": backup_result, "canonical": canonical_result, "batch_id": batch_id}
