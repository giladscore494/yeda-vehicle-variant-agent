from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
import copy
import uuid
from threading import Lock
from typing import Callable
from urllib.parse import quote

import requests
from core.ingest import load_model_seeds
from agent.runner import run_single_model
from storage.json_store import get_output_paths, load_json_list, load_json_object, save_json, project_root
from core.final_export_builder import build_clean_final_export, assert_no_mock_in_final_export, is_mock_contaminated_variant
from storage.github_canonical_store import fetch_file_from_github, push_canonical_resume_package, get_github_config

BATCH_STATE_SCHEMA = "batch_state_v1"

# ---------------------------------------------------------------------------
# Field helpers — handle both plain scalars and wrapped field objects such as
# {"value": 2008, "used_in_compare": false}
# ---------------------------------------------------------------------------

def scalar_value(x, default=None):
    """Return the scalar from a plain value or a wrapped field dict."""
    if isinstance(x, dict):
        return x.get("value", default)
    return x if x is not None else default


def safe_int_value(x, default=0):
    """Return int from plain value or wrapped field dict; return default on error."""
    try:
        return int(scalar_value(x, default) or default)
    except Exception:
        return int(default)


ALLOWED_NO_VARIANTS_REASONS = {
    "model_not_sold_in_market",
    "no_reliable_sources_found",
    "insufficient_grounded_data",
    "duplicate_existing_variant_only",
    "seed_out_of_scope",
    "model_discontinued_before_market_period",
    "source_conflict_unresolved",
    "blocked_by_validation",
}


def _ensure_zero_variant_fields(state: dict) -> dict:
    state.setdefault("needs_retry_seed_ids", [])
    state.setdefault("zero_variant_seed_ids", [])
    state.setdefault("false_processed_seed_ids", [])
    state.setdefault("seed_accounting", {})
    state.setdefault("no_variants_by_seed", {})
    state.setdefault("dedupe_proof_by_seed", {})
    state.setdefault("zero_variant_policy", "retry_then_block")
    state.setdefault("_last_queue_seed_ids", [])
    state.setdefault("_last_total_attempts", -1)
    return state


def _total_accounting_attempts(state: dict) -> int:
    """Return total cumulative attempts recorded in seed_accounting."""
    return sum(int(a.get("attempts", 0) or 0) for a in (state.get("seed_accounting") or {}).values())


def _load_variants_from_package(package: dict) -> list[dict]:
    acc=(package.get("accumulated_clean_export") or {}) if isinstance(package,dict) else {}
    if isinstance(acc.get("variants"), list):
        return [v for v in acc.get("variants",[]) if isinstance(v,dict)]
    fin=(package.get("final_export") or {}) if isinstance(package,dict) else {}
    if isinstance(fin.get("variants"), list):
        return [v for v in fin.get("variants",[]) if isinstance(v,dict)]
    if isinstance(package.get("variants"), list):
        return [v for v in package.get("variants",[]) if isinstance(v,dict)]
    return [v for v in (package.get("verified_variants") or []) + (package.get("partial_variants") or []) if isinstance(v,dict)]


def can_mark_seed_processed(seed_id: str, accounting: dict) -> dict:
    issues=[]
    added=int(accounting.get("variants_added_to_canonical",0) or 0)
    deduped=int(accounting.get("variants_deduped_or_merged",0) or 0)
    proof=accounting.get("dedupe_proof") or []
    reason=accounting.get("no_variants_reason")
    if added>0:
        return {"allowed":True,"reason":"variants_added","issues":issues}
    if deduped>0 and len(proof)>0:
        return {"allowed":True,"reason":"deduped_with_proof","issues":issues}
    if reason in ALLOWED_NO_VARIANTS_REASONS:
        return {"allowed":True,"reason":"no_variants_reason","issues":issues}
    if int(accounting.get("candidates_returned",0) or 0)==0: issues.append("candidates_returned==0")
    if int(accounting.get("valid_variants_built",0) or 0)==0: issues.append("valid_variants_built==0")
    if added==0: issues.append("variants_added_to_canonical==0")
    if deduped==0: issues.append("variants_deduped_or_merged==0")
    if reason is None: issues.append("no_variants_reason is null")
    return {"allowed":False,"reason":"zero_variants_without_explanation","issues":issues}


def find_processed_zero_variant_seeds(package: dict, ordered_seeds: list[dict] | None = None) -> list[dict]:
    package=package if isinstance(package,dict) else {}
    bs=(package.get("batch_state") or {}) if isinstance(package.get("batch_state"),dict) else {}
    processed=list(bs.get("processed_seed_ids") or [])
    seeds=ordered_seeds or package.get("ordered_seeds") or get_ordered_seed_list("IL")
    by={s.get("seed_id"):s for s in seeds if isinstance(s,dict) and s.get("seed_id")}
    variants=_load_variants_from_package(package)
    dedupe=bs.get("dedupe_proof_by_seed") or {}
    novar=bs.get("no_variants_by_seed") or {}
    out=[]
    for sid in processed:
        seed=by.get(sid,{"seed_id":sid})
        matched=[]
        for v in variants:
            if sid in {v.get("seed_id"),v.get("source_seed_id"),v.get("seed_ref")}:
                matched.append(v); continue
            if not seed: continue
            if str(scalar_value(v.get("make"),"")).strip().lower()!=str(seed.get("make","")).strip().lower(): continue
            if str(scalar_value(v.get("model"),"")).strip().lower()!=str(seed.get("model","")).strip().lower(): continue
            if str(scalar_value(v.get("market"),"")).strip().lower()!=str(seed.get("market","IL")).strip().lower(): continue
            sys=safe_int_value(seed.get("year_start"),0); sye=safe_int_value(seed.get("year_end"),9999)
            vys=safe_int_value(v.get("year_start"),0); vye=safe_int_value(v.get("year_end"),9999)
            if sys<=vye and sye>=vys: matched.append(v)
        reason=(novar.get(sid) or {}).get("reason") if isinstance(novar.get(sid),dict) else None
        proof=bool((dedupe.get(sid) or {}).get("matched_variant_ids")) if isinstance(dedupe.get(sid),dict) else False
        if len(matched)==0 and not proof and reason not in ALLOWED_NO_VARIANTS_REASONS:
            out.append({"seed_id":sid,"make":seed.get("make"),"model":seed.get("model"),"year_start":seed.get("year_start"),"year_end":seed.get("year_end"),"matched_variants_count":0,"dedupe_proof_found":False,"no_variants_reason":reason,"repair_status":"needs_retry"})
    return out

RETRYABLE_SCHEMA_ERROR_TOKENS = ["'market'", "missing market", "keyerror: market", "missing required seed field"]
CANONICAL_RESUME_PATH_DEFAULT = "data/canonical/resume_package_canonical.json"
CANONICAL_BACKUP_PATH_DEFAULT = "data/canonical/resume_package_backup_previous.json"
EXPECTED_CANONICAL_RESUME_PATH = "data/canonical/resume_package_canonical.json"
EXPECTED_LOCAL_LAST_COMPLETED_SEED_ID = "audi__rs5__2010__2026__il"
EXPECTED_LOCAL_NEXT_SEED_ID = "audi__rs6__2008__2026__il"
EXPECTED_LOCAL_MIN_VARIANTS = 263
EXPECTED_LOCAL_MIN_PROCESSED = 59
CANDIDATE_SOURCE_MERGED = "merged_candidate"
_LAST_CANONICAL_UPDATE_ATTEMPT = {
    "failed": False,
    "guard_issues": [],
    "candidate_variant_count": 0,
    "previous_variant_count": 0,
    "candidate_processed_count": 0,
    "previous_processed_count": 0,
    "candidate_source": None,
}
_LAST_CANONICAL_UPDATE_ATTEMPT_LOCK = Lock()


def _set_last_canonical_update_attempt(
    failed: bool,
    validate_result: dict | None = None,
    candidate_source: str | None = None,
):
    result = validate_result if isinstance(validate_result, dict) else {}
    with _LAST_CANONICAL_UPDATE_ATTEMPT_LOCK:
        _LAST_CANONICAL_UPDATE_ATTEMPT.update(
            {
                "failed": bool(failed),
                "guard_issues": list(result.get("issues") or []),
                "candidate_variant_count": int(result.get("candidate_variant_count", 0) or 0),
                "previous_variant_count": int(result.get("previous_variant_count", 0) or 0),
                "candidate_processed_count": int(result.get("candidate_processed_count", 0) or 0),
                "previous_processed_count": int(result.get("previous_processed_count", 0) or 0),
                "candidate_source": candidate_source or result.get("candidate_source"),
            }
        )


def get_last_canonical_update_attempt() -> dict:
    with _LAST_CANONICAL_UPDATE_ATTEMPT_LOCK:
        return copy.deepcopy(_LAST_CANONICAL_UPDATE_ATTEMPT)


def _token_prefix_type(token: str | None) -> str:
    """Classify GitHub token format as missing/github_pat/ghp/unknown without exposing token content."""
    value = str(token or "").strip()
    if not value:
        return "missing"
    if value.startswith("github_pat_"):
        return "github_pat"
    if value.startswith("ghp_"):
        return "ghp"
    return "unknown"


def _safe_json(resp) -> dict:
    try:
        payload = resp.json()
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _api_get(url: str, token: str | None) -> dict:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = requests.get(url, headers=headers, timeout=30)
    except Exception as exc:
        return {"status_code": None, "ok": False, "json": {}, "message": f"{type(exc).__name__}: {exc}"}
    payload = _safe_json(resp)
    message = payload.get("message")
    return {
        "status_code": resp.status_code,
        "ok": resp.status_code < 400,
        "json": payload,
        "message": str(message)[:200] if message else "",
    }


def _api_get_text(url: str, token: str | None) -> dict:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = requests.get(url, headers=headers, timeout=30)
    except Exception as exc:
        return {"status_code": None, "ok": False, "text": "", "message": f"{type(exc).__name__}: {exc}"}
    return {
        "status_code": resp.status_code,
        "ok": resp.status_code < 400,
        "text": resp.text if isinstance(resp.text, str) else "",
        "message": "",
    }


def _extract_package_fields(payload: dict | None) -> dict:
    package = payload if isinstance(payload, dict) else {}
    batch_state = package.get("batch_state", {}) if isinstance(package.get("batch_state"), dict) else {}
    processed = list(batch_state.get("processed_seed_ids") or [])
    return {
        "schema_version": package.get("schema_version"),
        "variant_count": canonical_variant_count(package),
        "processed_count": len(processed),
        "last_completed_seed_id": batch_state.get("last_completed_seed_id"),
        "next_seed_id": batch_state.get("next_seed_id"),
    }


def _is_next_seed_at_or_after_expected(next_seed_id: str | None, market: str = "IL") -> bool:
    if not next_seed_id:
        return False
    if next_seed_id == EXPECTED_LOCAL_NEXT_SEED_ID:
        return True
    try:
        ordered_ids = [s["seed_id"] for s in get_ordered_seed_list(market)]
    except Exception:
        ordered_ids = []
    if EXPECTED_LOCAL_NEXT_SEED_ID in ordered_ids and next_seed_id in ordered_ids:
        return ordered_ids.index(next_seed_id) >= ordered_ids.index(EXPECTED_LOCAL_NEXT_SEED_ID)
    return str(next_seed_id).strip().lower() >= EXPECTED_LOCAL_NEXT_SEED_ID


def diagnose_canonical_github_sync() -> dict:
    cfg = get_github_config()
    token = str(cfg.get("token") or "")
    repo = str(cfg.get("repo") or "")
    branch = str(cfg.get("branch") or "")
    canonical_path = str(cfg.get("canonical_path") or "")
    backup_path = str(cfg.get("backup_path") or "")

    token_present = bool(token.strip())
    token_length_gt_20 = len(token.strip()) > 20
    token_prefix_type = _token_prefix_type(token)
    token_missing = not token_present

    config = {
        "repo_value": repo,
        "branch_value": branch,
        "canonical_path_value": canonical_path,
        "backup_path_value": backup_path,
        "repo_has_slash": "/" in repo,
        "branch_not_empty": bool(branch.strip()),
        "canonical_path_matches_expected": canonical_path == EXPECTED_CANONICAL_RESUME_PATH,
        "expected_canonical_path": EXPECTED_CANONICAL_RESUME_PATH,
    }

    local_path = project_root() / EXPECTED_CANONICAL_RESUME_PATH
    local_exists = local_path.exists()
    local_payload = None
    local_is_valid_json = False
    if local_exists:
        try:
            local_payload = json.loads(local_path.read_text(encoding="utf-8"))
            local_is_valid_json = isinstance(local_payload, dict)
        except Exception:
            local_payload = None
            local_is_valid_json = False
    local_fields = _extract_package_fields(local_payload if local_is_valid_json else {})
    local_next_seed_valid = _is_next_seed_at_or_after_expected(local_fields.get("next_seed_id"), market="IL")
    local_expected_file = (
        local_exists
        and local_is_valid_json
        and local_fields.get("schema_version") == "resume_package_v1"
        and int(local_fields.get("variant_count", 0) or 0) >= EXPECTED_LOCAL_MIN_VARIANTS
        and int(local_fields.get("processed_count", 0) or 0) >= EXPECTED_LOCAL_MIN_PROCESSED
        and local_next_seed_valid
    )
    local_check = {
        "local_exists": local_exists,
        "local_is_valid_json": local_is_valid_json,
        "local_schema_version": local_fields.get("schema_version"),
        "local_variant_count": int(local_fields.get("variant_count", 0) or 0),
        "local_processed_count": int(local_fields.get("processed_count", 0) or 0),
        "local_last_completed_seed_id": local_fields.get("last_completed_seed_id"),
        "local_next_seed_id": local_fields.get("next_seed_id"),
        "local_expected_file": local_expected_file,
        "local_next_seed_at_or_after_expected": local_next_seed_valid,
        "expected_last_completed_seed_id": EXPECTED_LOCAL_LAST_COMPLETED_SEED_ID,
        "expected_next_seed_id": EXPECTED_LOCAL_NEXT_SEED_ID,
        "local_usage_note": "Local canonical is available and can be used even if GitHub fetch fails." if local_exists and local_is_valid_json else "",
    }

    api_contents_url = ""
    if repo and canonical_path and branch:
        api_contents_url = f"https://api.github.com/repos/{repo}/contents/{quote(canonical_path)}?ref={quote(branch)}"
    repo_api = {"status_code": None, "repo_access_ok": False, "visibility": None, "response_message_excerpt": ""}
    branch_api = {"status_code": None, "branch_exists": False, "default_branch": None, "response_message_excerpt": ""}
    contents_api = {
        "status_code": None,
        "github_file_exists": False,
        "github_file_size": None,
        "github_sha_exists": False,
        "github_download_url_exists": False,
        "github_is_valid_json": False,
        "github_parse_failed": False,
        "github_variant_count": 0,
        "github_processed_count": 0,
        "github_next_seed_id": None,
        "response_message_excerpt": "",
    }

    repo_result = _api_get(f"https://api.github.com/repos/{repo}", token) if repo else {"status_code": None, "ok": False, "json": {}, "message": "missing repo"}
    repo_body = repo_result.get("json", {})
    repo_api.update(
        {
            "status_code": repo_result.get("status_code"),
            "repo_access_ok": repo_result.get("status_code") == 200,
            "visibility": "private" if repo_body.get("private") else ("public" if repo_body else None),
            "response_message_excerpt": repo_result.get("message", ""),
        }
    )

    branch_result = {"status_code": None, "ok": False, "json": {}, "message": ""}
    if repo_result.get("status_code") == 200 and branch.strip():
        branch_result = _api_get(f"https://api.github.com/repos/{repo}/branches/{quote(branch)}", token)
        branch_api.update(
            {
                "status_code": branch_result.get("status_code"),
                "branch_exists": branch_result.get("status_code") == 200,
                "default_branch": repo_body.get("default_branch"),
                "response_message_excerpt": branch_result.get("message", ""),
            }
        )

    contents_result = {"status_code": None, "ok": False, "json": {}, "message": ""}
    github_payload = None
    if repo_result.get("status_code") == 200 and branch_result.get("status_code") == 200 and canonical_path.strip():
        contents_result = _api_get(f"https://api.github.com/repos/{repo}/contents/{quote(canonical_path)}?ref={quote(branch)}", token)
        content_body = contents_result.get("json", {})
        github_payload = None
        github_parse_failed = False
        if contents_result.get("status_code") == 200:
            encoded = content_body.get("content")
            encoding = content_body.get("encoding")
            download_url = content_body.get("download_url")
            if isinstance(encoded, str) and encoded.strip() and str(encoding or "").lower() == "base64":
                try:
                    github_payload = json.loads(base64.b64decode(encoded).decode("utf-8"))
                except Exception:
                    github_payload = None
                    github_parse_failed = True
            elif isinstance(encoded, str) and encoded.strip():
                github_parse_failed = True
            if not isinstance(github_payload, dict) and isinstance(download_url, str) and download_url.strip():
                raw_result = _api_get_text(download_url, token)
                if raw_result.get("status_code") == 200:
                    try:
                        raw_payload = json.loads(raw_result.get("text") or "{}")
                    except Exception:
                        raw_payload = None
                    if isinstance(raw_payload, dict):
                        github_payload = raw_payload
                        github_parse_failed = False
                    else:
                        github_parse_failed = True
                elif raw_result.get("status_code") is not None:
                    github_parse_failed = True
            if not isinstance(github_payload, dict):
                github_parse_failed = True
        github_fields = _extract_package_fields(github_payload if isinstance(github_payload, dict) else {})
        contents_api.update(
            {
                "status_code": contents_result.get("status_code"),
                "github_file_exists": contents_result.get("status_code") == 200,
                "github_file_size": content_body.get("size"),
                "github_sha_exists": bool(content_body.get("sha")),
                "github_download_url_exists": bool(content_body.get("download_url")),
                "github_is_valid_json": isinstance(github_payload, dict),
                "github_parse_failed": bool(contents_result.get("status_code") == 200 and github_parse_failed),
                "github_variant_count": int(github_fields.get("variant_count", 0) or 0),
                "github_processed_count": int(github_fields.get("processed_count", 0) or 0),
                "github_next_seed_id": github_fields.get("next_seed_id"),
                "response_message_excerpt": contents_result.get("message", ""),
            }
        )

    local_fallback_mode_active = bool(
        not contents_api.get("github_file_exists")
        and local_check.get("local_expected_file")
        and local_check.get("local_next_seed_at_or_after_expected")
    )

    manual_push_uses_local_canonical = True
    manual_push_rebuilds_package = False
    push_behavior = {
        "manual_push_uses_local_canonical": manual_push_uses_local_canonical,
        "manual_push_rebuilds_package": manual_push_rebuilds_package,
        "local_fallback_mode_active": local_fallback_mode_active,
    }

    previous_count_unknown_due_to_parse_error = False
    if contents_api.get("github_file_exists"):
        if contents_api.get("github_is_valid_json"):
            previous_count = int(contents_api.get("github_variant_count", 0) or 0)
        elif local_fallback_mode_active:
            previous_count = int(local_check.get("local_variant_count", 0) or 0)
        else:
            previous_count = 0
            previous_count_unknown_due_to_parse_error = True
    else:
        previous_count = int(local_check.get("local_variant_count", 0) or 0)

    build_final_export_count = None
    build_final_export_error = ""
    try:
        build_export = build_final_export()
        build_final_export_count = len([v for v in build_export.get("variants", []) if isinstance(v, dict)]) if isinstance(build_export, dict) else 0
    except Exception as exc:
        build_final_export_error = f"{type(exc).__name__}: {exc}"
    new_candidate_count = None
    build_resume_error = ""
    try:
        resume_package = build_resume_package()
        new_candidate_count = len(_extract_resume_variants(resume_package))
    except Exception as exc:
        build_resume_error = f"{type(exc).__name__}: {exc}"
        if build_final_export_count is not None:
            new_candidate_count = build_final_export_count
    uploaded_session_count = len(load_imported_accumulated_variants())
    shrink_detected = bool(
        previous_count_unknown_due_to_parse_error
        or (previous_count > 0 and isinstance(new_candidate_count, int) and new_candidate_count < previous_count)
    )
    shrink_delta = (int(new_candidate_count) - int(previous_count)) if isinstance(new_candidate_count, int) and not previous_count_unknown_due_to_parse_error else None
    shrink_guard = {
        "previous_count": previous_count,
        "previous_count_unknown_due_to_parse_error": previous_count_unknown_due_to_parse_error,
        "new_candidate_count": new_candidate_count,
        "build_final_export_count": build_final_export_count,
        "uploaded_session_canonical_count": uploaded_session_count,
        "shrink_detected": shrink_detected,
        "shrink_delta": shrink_delta,
        "build_final_export_error": build_final_export_error,
        "build_resume_package_error": build_resume_error,
    }

    permissions = {
        "repo_403_permission_or_rate_limit": repo_result.get("status_code") == 403,
        "contents_403_lacks_contents_permission": contents_result.get("status_code") == 403,
    }
    github_exists = bool(contents_api.get("github_file_exists"))
    github_valid = bool(contents_api.get("github_is_valid_json"))
    github_counts_ok = bool(
        (not github_exists)
        or (
            int(contents_api.get("github_variant_count", 0) or 0) >= EXPECTED_LOCAL_MIN_VARIANTS
            and int(contents_api.get("github_processed_count", 0) or 0) >= EXPECTED_LOCAL_MIN_PROCESSED
            and bool(contents_api.get("github_next_seed_id"))
        )
    )

    ruled_out: list[str] = []
    if token_present and token_length_gt_20:
        ruled_out.append("Token is not missing: token_present=true and token_length_gt_20=true.")
    if config.get("repo_has_slash"):
        ruled_out.append("Repository format is not the issue: GITHUB_REPO contains '/'.")
    if config.get("branch_not_empty"):
        ruled_out.append("Branch is not empty in secrets.")
    if repo_result.get("status_code") == 200:
        ruled_out.append("Repo name is not the issue: GET /repos returned 200.")
    if branch_result.get("status_code") == 200:
        ruled_out.append(f"Branch is not the issue: GET /branches/{branch} returned 200.")
    if config.get("canonical_path_matches_expected"):
        ruled_out.append("Canonical path is not the issue: configured path matches expected.")
    if local_check.get("local_exists") and local_check.get("local_is_valid_json"):
        ruled_out.append(f"Local file is not missing: local canonical exists with {local_check.get('local_variant_count')} variants.")
    if contents_result.get("status_code") == 200:
        ruled_out.append("GitHub canonical file exists and is readable at configured path.")
    if not shrink_detected:
        ruled_out.append("Shrink guard condition is not active: candidate package is not smaller than previous.")

    last_attempt = get_last_canonical_update_attempt()
    last_update_attempt_failed = bool(last_attempt.get("failed"))
    last_update_guard_issues = list(last_attempt.get("guard_issues") or [])
    root_cause = ""
    final_diagnosis = ""
    recommended_action = ""

    if token_missing:
        root_cause = "GITHUB_TOKEN is missing or empty in Streamlit Secrets."
        final_diagnosis = root_cause
        recommended_action = "Set GITHUB_TOKEN in Streamlit Secrets and restart the app."
    elif repo_result.get("status_code") == 401:
        root_cause = "GitHub token is invalid or not loaded."
        final_diagnosis = root_cause
        recommended_action = "Replace GITHUB_TOKEN with a valid token and redeploy/restart."
    elif repo_result.get("status_code") == 404:
        root_cause = "GITHUB_REPO is wrong or token has no access to this repo."
        final_diagnosis = root_cause
        recommended_action = "Fix GITHUB_REPO and confirm token access to that repository."
    elif branch_result.get("status_code") == 404:
        root_cause = "GITHUB_BRANCH does not exist or is not accessible."
        final_diagnosis = root_cause
        recommended_action = "Set GITHUB_BRANCH to an existing branch with token access."
    elif not config.get("canonical_path_matches_expected"):
        root_cause = "CANONICAL_RESUME_PATH mismatch."
        final_diagnosis = root_cause
        recommended_action = f"Set CANONICAL_RESUME_PATH to {EXPECTED_CANONICAL_RESUME_PATH}."
    elif repo_result.get("status_code") == 200 and branch_result.get("status_code") == 200 and contents_result.get("status_code") == 404 and local_check.get("local_exists") and local_check.get("local_is_valid_json"):
        root_cause = "Canonical file is missing at the configured path on GitHub."
        final_diagnosis = "Canonical file is missing at configured path on GitHub, but local canonical exists and should be usable. Push should create the file."
        recommended_action = "Use the valid local canonical as source of truth, then push to create the missing GitHub file."
    elif repo_result.get("status_code") == 403:
        root_cause = "GitHub token lacks permission or is blocked/rate-limited."
        final_diagnosis = root_cause
        recommended_action = "Check token scopes, repository access, and API rate limits."
    elif contents_result.get("status_code") == 403:
        root_cause = "Token lacks Contents permission."
        final_diagnosis = root_cause
        recommended_action = "Grant Contents read/write permission to the token."
    elif contents_api.get("github_file_exists") and not contents_api.get("github_is_valid_json"):
        root_cause = "GitHub canonical exists but JSON parsing failed."
        final_diagnosis = "GitHub canonical file exists but could not be parsed as JSON. The GitHub Contents API metadata/download handling is broken."
        recommended_action = "Fix GitHub canonical loading to decode base64 content or use download_url raw JSON; do not continue batch until parsing is valid."
    elif contents_api.get("github_file_exists") and contents_api.get("github_is_valid_json") and not github_counts_ok:
        root_cause = "GitHub canonical exists but failed minimum integrity thresholds."
        final_diagnosis = "GitHub canonical exists but its counters/state are incomplete (variants/processed/next_seed_id)."
        recommended_action = "Push a valid canonical package with minimum counts and a non-null next_seed_id before continuing batch."
    elif manual_push_rebuilds_package and local_check.get("local_expected_file") and isinstance(build_final_export_count, int) and build_final_export_count < int(local_check.get("local_variant_count", 0) or 0):
        root_cause = "Manual push rebuilds from incomplete local outputs instead of pushing the valid local canonical."
        final_diagnosis = root_cause
        recommended_action = "Push the valid local canonical package directly, or merge new batch variants into canonical before push."
    elif shrink_detected:
        root_cause = "Shrink guard correctly blocked because candidate package is smaller than canonical."
        final_diagnosis = "Shrink guard correctly blocked a smaller candidate package."
        recommended_action = "Use local/GitHub canonical as source of truth and merge new batch variants into it."
    elif last_update_attempt_failed and last_update_guard_issues:
        if "candidate_variant_count < previous_variant_count" in last_update_guard_issues:
            root_cause = "Canonical update was blocked because candidate package is smaller than previous canonical."
            final_diagnosis = root_cause
            recommended_action = "Use local/GitHub canonical as source of truth and merge new output into canonical without shrinking."
        elif "candidate package has final_merged_count > canonical_count but batch_state did not advance" in last_update_guard_issues:
            root_cause = "Canonical update was blocked because local temporary variants exist without batch_state advancement."
            final_diagnosis = root_cause
            recommended_action = "Advance batch_state with canonical progression or remove untracked temporary output variants before pushing."
        elif any(
            issue in last_update_guard_issues
            for issue in [
                "candidate package missing batch_state.processed_seed_ids",
                "candidate_processed_count < previous_processed_count",
                "candidate_next_seed_id is already processed",
                "candidate_last_completed_seed_id moved backward",
            ]
        ):
            root_cause = "Canonical update was blocked because candidate batch_state is invalid."
            final_diagnosis = root_cause
            recommended_action = "Fix candidate batch_state fields (processed_seed_ids, last_completed_seed_id, next_seed_id) and retry."
        elif "manual push attempted to rebuild from incomplete local outputs instead of using local canonical" in last_update_guard_issues:
            root_cause = "Canonical update was blocked because manual push rebuilt an invalid package instead of pushing local canonical."
            final_diagnosis = root_cause
            recommended_action = "Push local canonical directly or merge into local canonical first."
        else:
            root_cause = "GitHub sync is reachable, but canonical update candidate failed validation."
            final_diagnosis = root_cause
            recommended_action = "Inspect last_update_guard_issues and fix candidate package before retrying push/update."
    elif not (local_check.get("local_exists") and local_check.get("local_is_valid_json")):
        root_cause = "Local canonical invalid/missing."
        final_diagnosis = root_cause
        recommended_action = "Restore or import a valid local canonical resume package."
    else:
        root_cause = "No blocking root cause detected."
        final_diagnosis = "Canonical GitHub sync checks passed with no blocking root cause detected."
        recommended_action = "Continue batch processing and keep canonical in sync after each successful batch."

    github_source_ok = bool((github_valid and github_counts_ok) or local_fallback_mode_active)
    manual_push_safe = bool(manual_push_uses_local_canonical and not manual_push_rebuilds_package)
    safe_to_continue_batch = bool(
        local_check.get("local_expected_file")
        and local_check.get("local_next_seed_at_or_after_expected")
        and github_source_ok
        and github_counts_ok
        and manual_push_safe
        and not shrink_detected
        and not last_update_attempt_failed
    )

    checks = {
        "secrets": {
            "token_present": token_present,
            "token_length_gt_20": token_length_gt_20,
            "token_prefix_type": token_prefix_type,
        },
        "config": config,
        "local_canonical": local_check,
        "github_api_url": {"contents_url": api_contents_url},
        "repo_api_auth": {
            "repo_status_code": repo_api.get("status_code"),
            "repo_access_ok": repo_api.get("repo_access_ok"),
            "repo_visibility": repo_api.get("visibility"),
            "response_message_excerpt": repo_api.get("response_message_excerpt"),
        },
        "branch_check": {
            "branch_status_code": branch_api.get("status_code"),
            "branch_exists": branch_api.get("branch_exists"),
            "default_branch": branch_api.get("default_branch"),
            "response_message_excerpt": branch_api.get("response_message_excerpt"),
        },
        "github_contents_check": {
            "contents_status_code": contents_api.get("status_code"),
            "github_file_exists": contents_api.get("github_file_exists"),
            "github_file_size": contents_api.get("github_file_size"),
            "github_sha_exists": contents_api.get("github_sha_exists"),
            "github_download_url_exists": contents_api.get("github_download_url_exists"),
            "github_is_valid_json": contents_api.get("github_is_valid_json"),
            "github_parse_failed": contents_api.get("github_parse_failed"),
            "github_variant_count": contents_api.get("github_variant_count"),
            "github_processed_count": contents_api.get("github_processed_count"),
            "github_next_seed_id": contents_api.get("github_next_seed_id"),
            "response_message_excerpt": contents_api.get("response_message_excerpt"),
        },
        "permissions": permissions,
        "push_behavior": push_behavior,
        "shrink_guard_diagnosis": shrink_guard,
    }

    return {
        "final_diagnosis": final_diagnosis,
        "single_root_cause": root_cause,
        "ruled_out": ruled_out,
        "checks": checks,
        "recommended_action": recommended_action,
        "safe_to_continue_batch": safe_to_continue_batch,
        "last_update_attempt_failed": last_update_attempt_failed,
        "last_update_guard_issues": last_update_guard_issues,
        "last_candidate_variant_count": int(last_attempt.get("candidate_variant_count", 0) or 0),
        "last_previous_variant_count": int(last_attempt.get("previous_variant_count", 0) or 0),
        "last_candidate_processed_count": int(last_attempt.get("candidate_processed_count", 0) or 0),
        "last_previous_processed_count": int(last_attempt.get("previous_processed_count", 0) or 0),
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_token(value: str) -> str:
    text = " ".join(str(value or "").strip().lower().split())
    return text.replace("/", "-").replace(" ", "_")


def build_seed_id(make: str, model: str, year_start: int, year_end: int, market: str = "IL") -> str:
    return f"{normalize_token(make)}__{normalize_token(model)}__{int(year_start)}__{int(year_end)}__{normalize_token(market)}"


def get_ordered_seed_list(market: str = "IL") -> list[dict]:
    seeds = load_model_seeds()
    ordered = sorted(seeds, key=lambda s: ((s.make or "").lower(), (s.model or "").lower(), int(s.year_start or 0), int(s.year_end or 0)))
    return [{"make": s.make, "model": s.model, "year_start": int(s.year_start or 0), "year_end": int(s.year_end or 0), "market": market, "seed_id": build_seed_id(s.make, s.model, int(s.year_start or 0), int(s.year_end or 0), market)} for s in ordered]


def seed_to_dict(seed: dict, default_market: str = "IL") -> dict:
    market = seed.get("market") or default_market
    sid = str(seed.get("seed_id") or "")
    make = seed.get("make")
    model = seed.get("model")
    year_start = seed.get("year_start")
    year_end = seed.get("year_end")
    if sid and make and model and year_start is not None and year_end is not None:
        expected = build_seed_id(make, model, year_start, year_end, market)
        if expected != sid and "__" in sid:
            parts = sid.split("__")
            if len(parts) >= 5:
                make = parts[0].replace("_", " ").title()
                model = parts[1].replace("_", " ").title()
                try:
                    year_start = int(parts[2]); year_end = int(parts[3])
                except Exception:
                    pass
                market = parts[4].upper() or market
    return {"seed_id": sid, "make": make, "model": model, "year_start": year_start, "year_end": year_end, "market": market}


def _batch_state_path():
    return project_root() / "data/output/batch_state.json"


def _canonical_resume_path():
    cfg = get_github_config()
    rel = cfg.get("canonical_path") or CANONICAL_RESUME_PATH_DEFAULT
    return project_root() / rel


def _canonical_backup_path():
    cfg = get_github_config()
    rel = cfg.get("backup_path") or CANONICAL_BACKUP_PATH_DEFAULT
    return project_root() / rel


def load_local_canonical_resume_package() -> dict | None:
    payload = load_json_object(_canonical_resume_path())
    return payload if isinstance(payload, dict) and payload else None


def save_local_canonical_resume_package(package: dict):
    path = _canonical_resume_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    save_json(path, package)


def save_local_canonical_backup(package: dict):
    path = _canonical_backup_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    save_json(path, package)


def _extract_resume_variants(payload: dict) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    variants: list[dict] = []
    buckets = [
        ((payload.get("accumulated_clean_export") or {}).get("variants") if isinstance(payload.get("accumulated_clean_export"), dict) else None),
        ((payload.get("final_export") or {}).get("variants") if isinstance(payload.get("final_export"), dict) else None),
        payload.get("variants"),
        payload.get("verified_variants"),
        payload.get("partial_variants"),
    ]
    for bucket in buckets:
        if isinstance(bucket, list):
            variants.extend([copy.deepcopy(v) for v in bucket if isinstance(v, dict)])
    return dedupe_variants_stable(variants)


def _extract_canonical_variant_bucket(payload: dict | None) -> list[dict]:
    package = payload if isinstance(payload, dict) else {}
    accumulated = package.get("accumulated_clean_export") if isinstance(package.get("accumulated_clean_export"), dict) else {}
    final_export = package.get("final_export") if isinstance(package.get("final_export"), dict) else {}
    if isinstance(accumulated.get("variants"), list):
        return [copy.deepcopy(v) for v in accumulated.get("variants") if isinstance(v, dict)]
    if isinstance(final_export.get("variants"), list):
        return [copy.deepcopy(v) for v in final_export.get("variants") if isinstance(v, dict)]
    if isinstance(package.get("variants"), list):
        return [copy.deepcopy(v) for v in package.get("variants") if isinstance(v, dict)]
    fallback = []
    if isinstance(package.get("verified_variants"), list):
        fallback.extend([copy.deepcopy(v) for v in package.get("verified_variants") if isinstance(v, dict)])
    if isinstance(package.get("partial_variants"), list):
        fallback.extend([copy.deepcopy(v) for v in package.get("partial_variants") if isinstance(v, dict)])
    return dedupe_variants_stable(fallback)


def canonical_variant_count(payload: dict | None) -> int:
    return len(_extract_canonical_variant_bucket(payload))


def _empty_coverage_by_make(ordered_seeds: list[dict]) -> dict:
    coverage = {}
    for seed in ordered_seeds:
        make = seed["make"]
        coverage.setdefault(make, {"total": 0, "processed": 0, "verified_variants": 0, "partial_variants": 0, "unresolved": 0, "failed": 0, "completed": False})
        coverage[make]["total"] += 1
    return coverage


def _default_state(market: str, ordered_seeds: list[dict]) -> dict:
    now = _now()
    return {"schema_version": BATCH_STATE_SCHEMA, "market": market, "created_at": now, "updated_at": now, "last_batch_id": None, "total_seeds": len(ordered_seeds), "processed_seed_ids": [], "failed_seed_ids": [], "skipped_seed_ids": [], "in_progress_seed_id": None, "last_completed_seed_id": None, "next_seed_id": ordered_seeds[0]["seed_id"] if ordered_seeds else None, "coverage_by_make": _empty_coverage_by_make(ordered_seeds), "run_history": [], "failed_details": []}


def extract_canonical_batch_state(package: dict, ordered_seeds: list[dict], market: str = "IL", strict_zero_variant_audit: bool = False) -> dict:
    canonical_by_id = {s["seed_id"]: seed_to_dict(s, default_market=market) for s in ordered_seeds}
    canonical_ids = [s["seed_id"] for s in ordered_seeds]
    canonical_set = set(canonical_ids)
    raw_state = package.get("batch_state") if isinstance(package, dict) and isinstance(package.get("batch_state"), dict) else {}

    incoming_ids = [sid for sid in (raw_state.get("processed_seed_ids") or []) if isinstance(sid, str) and sid in canonical_set]
    legacy_processed_ids = []
    if isinstance(raw_state.get("processed_seeds"), list):
        for row in raw_state.get("processed_seeds") or []:
            sid = row.get("seed_id") if isinstance(row, dict) else None
            if isinstance(sid, str):
                legacy_processed_ids.append(sid)
                if sid in canonical_set:
                    incoming_ids.append(sid)
    if len(incoming_ids) == 0 and len(legacy_processed_ids) > 0:
        variants = _extract_canonical_variant_bucket(package)
        for seed in ordered_seeds:
            smake = str(seed.get("make", "")).strip().lower()
            smodel = str(seed.get("model", "")).strip().lower()
            smarket = str(seed.get("market") or market or "IL").strip().lower()
            sys = safe_int_value(seed.get("year_start"), 0)
            sye = safe_int_value(seed.get("year_end"), 9999)
            if not any((smake in lp and smodel in lp) for lp in legacy_processed_ids):
                continue
            for v in variants:
                if str(scalar_value(v.get("make"), "")).strip().lower() != smake:
                    continue
                if str(scalar_value(v.get("model"), "")).strip().lower() != smodel:
                    continue
                vmarket = str(scalar_value(v.get("market"), market or "IL")).strip().lower()
                if vmarket != smarket:
                    continue
                vys = safe_int_value(v.get("year_start"), 0)
                vye = safe_int_value(v.get("year_end"), 9999)
                if sys <= vye and sye >= vys:
                    incoming_ids.append(seed["seed_id"])
                    break

    processed_set = set(incoming_ids)
    processed_seed_ids = [sid for sid in canonical_ids if sid in processed_set]

    raw_last_completed = raw_state.get("last_completed_seed_id")
    raw_next_seed = raw_state.get("next_seed_id")

    if len(processed_seed_ids) == 0 and str(package.get("schema_version") or "").startswith("resume_package") and raw_last_completed in canonical_set:
        processed_seed_ids = canonical_ids[: canonical_ids.index(raw_last_completed) + 1]
    if len(processed_seed_ids) == 0 and str(package.get("schema_version") or "").startswith("resume_package") and raw_next_seed in canonical_set:
        next_idx = canonical_ids.index(raw_next_seed)
        if next_idx > 0:
            processed_seed_ids = canonical_ids[:next_idx]

    processed_set = set(processed_seed_ids)
    next_seed_id = next_unprocessed_seed_id(canonical_ids, processed_set)
    if next_seed_id is None:
        contiguous_idx = len(canonical_ids) - 1
    else:
        contiguous_idx = canonical_ids.index(next_seed_id) - 1
    last_completed_seed_id = canonical_ids[contiguous_idx] if contiguous_idx >= 0 else None

    failed_seed_ids = [sid for sid in (raw_state.get("failed_seed_ids") or []) if sid in canonical_set and sid not in processed_set]
    skipped_seed_ids = [sid for sid in (raw_state.get("skipped_seed_ids") or []) if sid in canonical_set and sid not in processed_set]
    failed_details = [d for d in (raw_state.get("failed_details") or []) if isinstance(d, dict) and d.get("seed_id") not in processed_set]

    now = _now()
    normalized = {
        "schema_version": BATCH_STATE_SCHEMA,
        "market": raw_state.get("market") or market or "IL",
        "created_at": raw_state.get("created_at") or now,
        "updated_at": now,
        "last_batch_id": raw_state.get("last_batch_id"),
        "total_seeds": len(ordered_seeds),
        "processed_seed_ids": processed_seed_ids,
        "processed_seeds": [canonical_by_id[sid] for sid in processed_seed_ids],
        "failed_seed_ids": failed_seed_ids,
        "skipped_seed_ids": skipped_seed_ids,
        "in_progress_seed_id": None,
        "last_completed_seed_id": last_completed_seed_id,
        "next_seed_id": next_seed_id,
        "run_history": raw_state.get("run_history", []),
        "failed_details": failed_details,
    }
    _ensure_zero_variant_fields(normalized)
    if strict_zero_variant_audit:
        normalized["false_processed_seed_ids"] = find_processed_zero_variant_seeds(
            {"batch_state": normalized, "accumulated_clean_export": {"variants": _extract_canonical_variant_bucket(package)}},
            ordered_seeds=ordered_seeds,
        )
    _refresh_coverage(normalized, ordered_seeds)
    return normalized


def load_batch_state(market: str = "IL") -> dict:
    ordered = get_ordered_seed_list(market)
    state = load_json_object(_batch_state_path())
    if not state or state.get("schema_version") != BATCH_STATE_SCHEMA or state.get("market") != market:
        state = _default_state(market, ordered)
        save_json(_batch_state_path(), state)
    return state


def _save_state(state: dict):
    state["updated_at"] = _now()
    save_json(_batch_state_path(), state)


def _load_outputs() -> dict:
    p = get_output_paths()
    return {"run_history": load_json_list(p["run_history"]), "unresolved": load_json_list(p["unresolved_models"]), "conflicts": load_json_list(p["vehicle_conflicts"]), "verified": load_json_list(p["vehicle_variants_verified"]), "partial": load_json_list(p["vehicle_variants_partial"]), "sources": load_json_list(p["vehicle_sources"])}


def _is_verified_variant(variant: dict) -> bool:
    status = str(variant.get("verification_status") or variant.get("classification") or variant.get("status") or "").lower()
    return status == "verified"


def _field_value(variant: dict, field_name: str):
    value = variant.get(field_name)
    if isinstance(value, dict):
        return value.get("value", value)
    return value


def _norm_token(value) -> str:
    return str(value if value is not None else "").strip().lower()


def _variant_identity_key(variant: dict) -> str:
    parts = [
        _field_value(variant, "make"),
        _field_value(variant, "model"),
        _field_value(variant, "market"),
        _field_value(variant, "year_start"),
        _field_value(variant, "year_end"),
        _field_value(variant, "generation"),
        _field_value(variant, "body_type"),
        _field_value(variant, "seats"),
        _field_value(variant, "engine"),
        _field_value(variant, "transmission"),
        _field_value(variant, "fuel_type"),
        _field_value(variant, "drivetrain"),
        _field_value(variant, "trim"),
    ]
    return "|".join(_norm_token(p) for p in parts)


def _variant_dedupe_key(variant: dict) -> str | None:
    variant_id = _norm_token(variant.get("variant_id"))
    if variant_id:
        return f"id:{variant_id}"
    key = _variant_identity_key(variant)
    return f"identity:{key}" if key.replace("|", "").strip() else None


def _variant_status_rank(variant: dict) -> int:
    status = _norm_token(variant.get("verification_status") or variant.get("classification") or variant.get("status"))
    if status == "verified":
        return 2
    if status == "partial":
        return 1
    return 0


def _variant_completeness_score(variant: dict) -> int:
    fields = [
        "make",
        "model",
        "market",
        "year_start",
        "year_end",
        "generation",
        "body_type",
        "seats",
        "engine",
        "transmission",
        "fuel_type",
        "drivetrain",
        "trim",
    ]
    base = sum(1 for f in fields if _field_value(variant, f) not in (None, ""))
    source_ids = variant.get("source_ids") if isinstance(variant.get("source_ids"), list) else []
    source_score = len([sid for sid in source_ids if isinstance(sid, str) and sid.strip()])
    return base + source_score + int(variant.get("sources_count", 0) or 0)


def _unique_strings(items: list) -> list[str]:
    out = []
    seen = set()
    for item in items or []:
        if not isinstance(item, str):
            continue
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _trim_option_entries(variant: dict) -> list[dict]:
    opts = []
    for row in variant.get("trim_options") or []:
        if isinstance(row, dict):
            opts.append(copy.deepcopy(row))
    trim = _field_value(variant, "trim")
    trim_field = variant.get("trim")
    if trim not in (None, ""):
        item = {"value": trim}
        if isinstance(trim_field, dict):
            item["source_ids"] = _unique_strings(trim_field.get("source_ids") or trim_field.get("source_urls") or [])
            item["status"] = trim_field.get("status")
            item["sources_count"] = int(trim_field.get("sources_count", 0) or 0)
        opts.append(item)
    return opts


def _merge_trim_options(existing: dict, incoming: dict) -> list[dict]:
    merged = []
    seen = set()
    for row in _trim_option_entries(existing) + _trim_option_entries(incoming):
        key = (
            _norm_token(row.get("value")),
            tuple(sorted(_unique_strings(row.get("source_ids") or []))),
            _norm_token(row.get("status")),
            int(row.get("sources_count", 0) or 0),
        )
        if key in seen:
            continue
        seen.add(key)
        merged.append(row)
    return merged


def _is_real_full_variant(variant: dict) -> bool:
    if not isinstance(variant, dict):
        return False
    if not _norm_token(variant.get("variant_id")):
        return False
    required = ["make", "model", "market", "year_start", "year_end"]
    return all(_field_value(variant, f) not in (None, "") for f in required)


def _merge_variant_pair(current: dict, incoming: dict) -> dict:
    current_rank = _variant_status_rank(current)
    incoming_rank = _variant_status_rank(incoming)
    if incoming_rank > current_rank:
        primary, secondary = incoming, current
    elif incoming_rank < current_rank:
        primary, secondary = current, incoming
    else:
        incoming_score = _variant_completeness_score(incoming)
        current_score = _variant_completeness_score(current)
        primary, secondary = (incoming, current) if incoming_score > current_score else (current, incoming)
    merged = copy.deepcopy(secondary)
    merged.update(copy.deepcopy(primary))
    merged["source_ids"] = _unique_strings((secondary.get("source_ids") or []) + (primary.get("source_ids") or []))
    if int(secondary.get("sources_count", 0) or 0) > int(merged.get("sources_count", 0) or 0):
        merged["sources_count"] = int(secondary.get("sources_count", 0) or 0)
    if not merged.get("candidate_raw") and secondary.get("candidate_raw"):
        merged["candidate_raw"] = copy.deepcopy(secondary.get("candidate_raw"))
    trims = _merge_trim_options(secondary, primary)
    if trims:
        merged["trim_options"] = trims
    if current_rank == 2 or incoming_rank == 2:
        merged["verification_status"] = "verified"
    elif current_rank == 1 or incoming_rank == 1:
        merged["verification_status"] = merged.get("verification_status") or merged.get("classification") or "partial"
    return merged


def dedupe_variants_stable(variants: list[dict]) -> list[dict]:
    by_key: dict[str, dict] = {}
    for row in variants or []:
        if not isinstance(row, dict):
            continue
        key = _variant_dedupe_key(row)
        if key is None:
            continue
        current = by_key.get(key)
        if current is None:
            by_key[key] = copy.deepcopy(row)
            continue
        by_key[key] = _merge_variant_pair(current, row)
    return list(by_key.values())


def _merge_variant_lists(existing: list[dict], incoming: list[dict]) -> list[dict]:
    return dedupe_variants_stable([*(existing or []), *(incoming or [])])


def _split_variants(variants: list[dict]) -> tuple[list[dict], list[dict]]:
    verified = [v for v in variants if isinstance(v, dict) and _is_verified_variant(v)]
    partial = [v for v in variants if isinstance(v, dict) and not _is_verified_variant(v)]
    return verified, partial


def load_imported_accumulated_variants() -> list[dict]:
    imported_dataset = load_json_object(project_root() / "data/output/imported_accumulated_dataset.json")
    if not isinstance(imported_dataset, dict):
        return []
    variants: list[dict] = []
    buckets = [
        imported_dataset.get("variants"),
        (imported_dataset.get("accumulated_clean_export") or {}).get("variants") if isinstance(imported_dataset.get("accumulated_clean_export"), dict) else None,
        (imported_dataset.get("final_export") or {}).get("variants") if isinstance(imported_dataset.get("final_export"), dict) else None,
    ]
    for bucket in buckets:
        if not isinstance(bucket, list):
            continue
        variants.extend([copy.deepcopy(v) for v in bucket if isinstance(v, dict)])
    return dedupe_variants_stable(variants)


def _load_canonical_source_variants() -> tuple[list[dict], str]:
    local = load_local_canonical_resume_package()
    if isinstance(local, dict):
        local_variants = _extract_resume_variants(local)
        if local_variants:
            return local_variants, "local_canonical"
    cfg = get_github_config()
    github = fetch_file_from_github(cfg.get("canonical_path") or CANONICAL_RESUME_PATH_DEFAULT)
    if isinstance(github, dict):
        github_variants = _extract_resume_variants(github)
        if github_variants:
            save_local_canonical_resume_package(github)
            return github_variants, "github_canonical"
    imported = load_imported_accumulated_variants()
    if imported:
        return imported, "imported_accumulated_dataset"
    combined_clean = load_json_object(project_root() / "data/output/combined_vehicle_variants_final_clean.json")
    clean_variants = [v for v in combined_clean.get("variants", []) if isinstance(v, dict)] if isinstance(combined_clean, dict) else []
    if clean_variants:
        return dedupe_variants_stable(clean_variants), "combined_clean"
    return [], "missing"


def _extract_result_variants(result_row: dict) -> list[dict]:
    if not isinstance(result_row, dict):
        return []
    result = result_row.get("result") if isinstance(result_row.get("result"), dict) else result_row
    variants = []
    for key in ["variants", "verified_variants", "partial_variants", "accumulated_variants"]:
        bucket = result.get(key) if isinstance(result, dict) else None
        if isinstance(bucket, list):
            variants.extend([v for v in bucket if isinstance(v, dict)])
    parsed = (((result.get("trace") or {}).get("discovery_parsed_json_debug") or {}) if isinstance(result, dict) else {})
    candidate_variants = parsed.get("candidate_variants", []) if isinstance(parsed, dict) else []
    if isinstance(candidate_variants, list):
        variants.extend([v for v in candidate_variants if isinstance(v, dict) and _is_real_full_variant(v)])
    return [v for v in variants if _is_real_full_variant(v)]


def load_all_accumulated_variants() -> dict:
    paths = get_output_paths()
    inputs_loaded = {
        "canonical_resume_package": 0,
        "canonical_source": "missing",
        "imported_accumulated_dataset": 0,
        "combined_clean": 0,
        "combined_old": 0,
        "vehicle_variants_verified": 0,
        "vehicle_variants_partial": 0,
        "run_history_embedded": 0,
        "latest_batch_full_variants": 0,
    }
    merged_variants: list[dict] = []

    canonical_variants, canonical_source = _load_canonical_source_variants()
    inputs_loaded["canonical_resume_package"] = len(canonical_variants)
    inputs_loaded["canonical_source"] = canonical_source
    merged_variants.extend(canonical_variants)

    imported_variants = [v for v in load_imported_accumulated_variants() if isinstance(v, dict)]
    inputs_loaded["imported_accumulated_dataset"] = len(imported_variants)
    merged_variants.extend(imported_variants)

    combined_clean = load_json_object(project_root() / "data/output/combined_vehicle_variants_final_clean.json")
    clean_variants = [v for v in combined_clean.get("variants", []) if isinstance(v, dict)] if isinstance(combined_clean, dict) else []
    inputs_loaded["combined_clean"] = len(clean_variants)
    merged_variants.extend(clean_variants)

    combined_old = load_json_object(project_root() / "data/output/combined_vehicle_variants_final.json")
    old_variants = [v for v in combined_old.get("variants", []) if isinstance(v, dict)] if isinstance(combined_old, dict) else []
    inputs_loaded["combined_old"] = len(old_variants)
    merged_variants.extend(old_variants)

    verified = [v for v in load_json_list(paths["vehicle_variants_verified"]) if isinstance(v, dict)]
    partial = [v for v in load_json_list(paths["vehicle_variants_partial"]) if isinstance(v, dict)]
    inputs_loaded["vehicle_variants_verified"] = len(verified)
    inputs_loaded["vehicle_variants_partial"] = len(partial)
    merged_variants.extend(verified)
    merged_variants.extend(partial)

    run_history = load_json_list(paths["run_history"])
    history_variants = []
    for run in run_history:
        if not isinstance(run, dict):
            continue
        history_variants.extend([v for v in (run.get("variants") or []) if isinstance(v, dict) and _is_real_full_variant(v)])
    inputs_loaded["run_history_embedded"] = len(history_variants)
    merged_variants.extend(history_variants)

    latest = load_json_object(project_root() / "data/output/latest_batch_result.json")
    latest_variants = []
    for row in latest.get("results", []) if isinstance(latest, dict) else []:
        latest_variants.extend(_extract_result_variants(row))
    inputs_loaded["latest_batch_full_variants"] = len(latest_variants)
    merged_variants.extend(latest_variants)

    sources = load_json_list(paths["vehicle_sources"])
    deduped = [v for v in dedupe_variants_stable(merged_variants) if isinstance(v, dict) and not is_mock_contaminated_variant(v)]
    verified, partial = _split_variants(deduped)
    return {"verified": verified, "partial": partial, "sources": sources, "inputs_loaded": inputs_loaded}


def is_seed_completed(seed_id: str, outputs: dict, batch_state: dict) -> bool:
    if seed_id in (batch_state.get("processed_seed_ids") or []):
        return True
    for row in outputs.get("unresolved", []):
        if row.get("seed_id") == seed_id:
            return True
    for row in outputs.get("conflicts", []):
        if row.get("seed_id") == seed_id:
            return True
    for run in outputs.get("run_history", []):
        if run.get("seed_id") != seed_id:
            continue
        if run.get("status") != "completed":
            continue
        summary = run.get("classification_summary") or {}
        if any(int(summary.get(k, run.get(k, 0)) or 0) > 0 for k in ["verified_count", "partial_count", "conflict_count", "unresolved_count"]):
            return True
        if run.get("variants_created") is not None:
            return True
    return False


def audit_coverage_until_last_completed(ordered_seeds: list[dict], batch_state: dict, outputs: dict) -> dict:
    seed_ids = [s["seed_id"] for s in ordered_seeds]
    last_completed_seed_id = batch_state.get("last_completed_seed_id")
    if last_completed_seed_id not in seed_ids:
        # fallback: furthest processed in canonical order
        completed_set = set(batch_state.get("processed_seed_ids") or [])
        idxs = [i for i, s in enumerate(seed_ids) if s in completed_set]
        last_idx = max(idxs) if idxs else -1
        last_completed_seed_id = seed_ids[last_idx] if last_idx >= 0 else None
    else:
        last_idx = seed_ids.index(last_completed_seed_id)
    if last_completed_seed_id is None:
        last_idx = -1
    missing = []
    for seed in ordered_seeds[: last_idx + 1]:
        if not is_seed_completed(seed["seed_id"], outputs, batch_state):
            missing.append(seed_to_dict(seed))
    return {"last_completed_seed_id": last_completed_seed_id, "last_completed_index": last_idx, "scanned_count": max(last_idx + 1, 0), "missing_seed_ids": [m["seed_id"] for m in missing], "missing_seeds": missing, "holes_count": len(missing), "coverage_complete_until_last_completed": len(missing) == 0}


def _refresh_coverage(state: dict, ordered_seeds: list[dict]):
    coverage = _empty_coverage_by_make(ordered_seeds)
    by_seed = {s["seed_id"]: s for s in ordered_seeds}
    for sid in state.get("processed_seed_ids", []):
        if sid in by_seed:
            coverage[by_seed[sid]["make"]]["processed"] += 1
    for sid in state.get("failed_seed_ids", []):
        if sid in by_seed:
            coverage[by_seed[sid]["make"]]["failed"] += 1
    for run in load_json_list(get_output_paths()["run_history"]):
        make = run.get("make")
        if make in coverage:
            summary = run.get("classification_summary") or {}
            coverage[make]["verified_variants"] += int(summary.get("verified_count", run.get("verified_count", 0)) or 0)
            coverage[make]["partial_variants"] += int(summary.get("partial_count", run.get("partial_count", 0)) or 0)
            coverage[make]["unresolved"] += int(summary.get("unresolved_count", run.get("unresolved_count", 0)) or 0)
    for make, c in coverage.items():
        c["completed"] = c["processed"] >= c["total"] and c["total"] > 0
    state["coverage_by_make"] = coverage




def process_seed_with_variant_retry(seed: dict, state: dict | None = None, max_attempts: int = 3, market: str = "IL", use_cache: bool = True, force_refresh: bool = False) -> dict:
    capped=max(1,min(int(max_attempts or 3),5))
    st = _ensure_zero_variant_fields(state if isinstance(state,dict) else {})
    sid=seed.get("seed_id")
    last=None
    for attempt in range(1,capped+1):
        # Part E: on attempt 2+ send the retry prompt that demands a no_variants_reason
        retry_hint = attempt > 1
        result = run_single_model(seed["make"], seed["model"], seed["year_start"], seed["year_end"], market=seed.get("market") or market, use_cache=use_cache, force_refresh=force_refresh, retry_hint=retry_hint)
        last=result
        trace=(result.get("trace") or {}) if isinstance(result,dict) else {}
        candidates=int(trace.get("candidate_variants_count",0) or 0)
        valid=int(result.get("variants_created", trace.get("variants_created",0)) or 0)
        added=max(int(result.get("verified_count",0) or 0)+int(result.get("partial_count",0) or 0), int(result.get("variants_created",0) or 0))
        no_reason=((trace.get("discovery_parsed_json_debug") or {}).get("no_variants_reason") if isinstance(trace.get("discovery_parsed_json_debug"),dict) else None)
        accounting={"seed_id":sid,"batch_id":st.get("last_batch_id"),"attempts":attempt,"candidates_returned":candidates,"valid_variants_built":valid,"variants_added_to_canonical":added,"variants_deduped_or_merged":0,"dedupe_proof":[],"no_variants_reason":no_reason,"marked_processed":False,"status":"needs_retry","failure_reason":"zero_variants_without_explanation"}
        decision=can_mark_seed_processed(sid,accounting)
        if decision["allowed"]:
            accounting["marked_processed"]=True
            accounting["status"]="processed_added" if added>0 else ("processed_deduped" if accounting["variants_deduped_or_merged"]>0 else "processed_no_variants_reason")
            st.setdefault("processed_seed_ids",[])
            if sid not in st["processed_seed_ids"]: st["processed_seed_ids"].append(sid)
            if sid in st.get("needs_retry_seed_ids",[]): st["needs_retry_seed_ids"].remove(sid)
            if no_reason in ALLOWED_NO_VARIANTS_REASONS:
                st.setdefault("no_variants_by_seed",{})[sid]={"reason":no_reason,"attempts":attempt,"updated_at":_now(),"sources_checked":[]}
            st.setdefault("seed_accounting",{})[sid]=accounting
            return {**result,"status":"completed","accounting":accounting}
    st.setdefault("needs_retry_seed_ids",[])
    if sid not in st["needs_retry_seed_ids"]: st["needs_retry_seed_ids"].append(sid)
    fail={"seed_id":sid,"batch_id":st.get("last_batch_id"),"attempts":capped,"candidates_returned":0,"valid_variants_built":0,"variants_added_to_canonical":0,"variants_deduped_or_merged":0,"dedupe_proof":[],"no_variants_reason":None,"marked_processed":False,"status":"failed_after_retries","failure_reason":"zero_variants_without_explanation_after_retries"}
    st.setdefault("seed_accounting",{})[sid]=fail
    return {**(last or {}),"status":"failed_after_retries","accounting":fail,"blocked":True,"message":"Seed processing blocked: no variants added, no dedupe proof, and no no_variants_reason."}

def _process_seeds(
    seed_queue: list[dict],
    state: dict,
    ordered: list[dict],
    limit: int,
    force_refresh=False,
    use_cache=True,
    progress_callback: Callable | None = None,
    auto_push_per_seed: bool = False,
    commit_message_prefix: str = "Update canonical vehicle variants",
    market: str = "IL",
    batch_mode: str = "resume_forward",
) -> tuple[list, list, list]:
    """Process a batch of seeds and return (results, per_seed_canonical, execution_trace).

    After each successfully completed seed the canonical package is saved
    locally (mandatory) and, when auto_push_per_seed=True, also pushed to
    GitHub immediately with a commit message derived from commit_message_prefix.
    Failed, skipped, or mock-contaminated results are never persisted.

    For zero_variant_repair and needs_retry queues the cache is bypassed so
    that every attempt actually calls Gemini (unless the caller explicitly sets
    allow_cache_during_repair=True via the use_cache kwarg).
    """
    # Issue 1: Force fresh Gemini calls for repair/retry queues
    effective_force_refresh = force_refresh
    effective_use_cache = use_cache
    if batch_mode in {"zero_variant_repair", "needs_retry", "hole_repair"} and not force_refresh:
        effective_force_refresh = True
        effective_use_cache = False

    results = []
    per_seed_canonical: list[dict] = []
    execution_trace: list[dict] = []
    _ensure_zero_variant_fields(state)
    for idx, seed in enumerate(seed_queue[:limit], start=1):
        selected_market = state.get("market")
        seed["market"] = seed.get("market") or selected_market or "IL"
        seed_market = seed["market"]
        if not seed.get("seed_id"):
            seed["seed_id"] = build_seed_id(seed.get("make"), seed.get("model"), seed.get("year_start"), seed.get("year_end"), seed_market)
        sid = seed["seed_id"]
        attempt_before = int((state.get("seed_accounting") or {}).get(sid, {}).get("attempts", 0) or 0)
        state["in_progress_seed_id"] = sid
        _save_state(state)
        if progress_callback:
            progress_callback({"index": idx, "total": min(limit, len(seed_queue)), "seed": seed, "results": list(results)})
        processor_called = False
        try:
            result = process_seed_with_variant_retry(seed, state=state, max_attempts=3, market=seed["market"], use_cache=effective_use_cache, force_refresh=effective_force_refresh)
            processor_called = True
        except Exception as exc:
            result = {"status": "error", "error": str(exc)}
        status = result.get("status")
        accounting = result.get("accounting") or {}
        # Extract real Gemini call metadata from trace
        result_trace = result.get("trace") or {}
        actual_gemini_call = bool(result_trace.get("gemini_attempted")) and not bool(result_trace.get("final_cache_hit"))
        final_cache_hit = bool(result_trace.get("final_cache_hit"))
        discovery_cache_hit = bool(result_trace.get("discovery_cache_hit"))
        if status == "error" and sid not in state["failed_seed_ids"]:
            state["failed_seed_ids"].append(sid)
            state.setdefault("failed_details", []).append({"seed_id": sid, "reason": str(result.get("error", "")), "created_at": _now()})
        if status == "failed_after_retries" and sid not in state["failed_seed_ids"]:
            state["failed_seed_ids"].append(sid)
            state.setdefault("failed_details", []).append({"seed_id": sid, "reason": accounting.get("failure_reason", "failed_after_retries"), "created_at": _now()})
        if status in {"completed", "partial"}:
            state["last_completed_seed_id"] = sid
        state["in_progress_seed_id"] = None
        results.append({"seed": seed, "result": result})
        _refresh_coverage(state, ordered)
        _save_state(state)
        # Per-seed canonical persistence: local save is mandatory; GitHub push is optional.
        # Issue 4: also persist when failed_after_retries to record seed_accounting in canonical
        saved_canonical = False
        if status in {"completed", "partial"}:
            seed_persist = persist_canonical_after_seed(
                seed=seed,
                batch_state=copy.deepcopy(state),
                push_to_github=auto_push_per_seed,
                commit_message_prefix=commit_message_prefix,
                market=seed_market,
            )
            per_seed_canonical.append({"seed_id": sid, "canonical_persist": seed_persist})
            saved_canonical = bool(isinstance(seed_persist, dict) and seed_persist.get("ok"))
        elif status == "failed_after_retries":
            # Persist batch_state fields (seed_accounting, failed_seed_ids) into canonical
            # without adding new variants, so the state survives canonical reloads.
            try:
                _persist_batch_state_into_canonical(copy.deepcopy(state), market=seed_market)
                saved_canonical = True
            except Exception:
                saved_canonical = False
        execution_trace.append({
            "seed_id": sid,
            "queue_reason": batch_mode,
            "attempt_before": attempt_before,
            "attempt_after": int(accounting.get("attempts", attempt_before) or attempt_before),
            "processor_called": processor_called,
            "did_call_model": processor_called,  # backward-compat alias
            "actual_gemini_call": actual_gemini_call,
            "final_cache_hit": final_cache_hit,
            "discovery_cache_hit": discovery_cache_hit,
            "force_refresh_used": effective_force_refresh,
            "use_cache_used": effective_use_cache,
            "model_response_received": processor_called and status != "error",
            "candidates_returned": int(accounting.get("candidates_returned", 0) or 0),
            "valid_variants_built": int(accounting.get("valid_variants_built", 0) or 0),
            "variants_added_to_canonical": int(accounting.get("variants_added_to_canonical", 0) or 0),
            "variants_deduped_or_merged": int(accounting.get("variants_deduped_or_merged", 0) or 0),
            "dedupe_proof_count": len(accounting.get("dedupe_proof") or []),
            "no_variants_reason": accounting.get("no_variants_reason"),
            "final_status": status,
            "saved_canonical": saved_canonical,
            "saved_batch_state": True,
        })
    return results, per_seed_canonical, execution_trace


def repair_false_processed_seeds(package: dict, ordered_seeds: list[dict] | None = None, market: str = "IL") -> dict:
    """Remove false-processed zero-variant seeds from processed_seed_ids and queue them for retry.

    A seed is considered false-processed when it is listed in processed_seed_ids but has
    matched_variants_count == 0, no dedupe_proof, and no no_variants_reason.

    Returns a dict with:
      ok, repaired_count, repaired_seed_ids, false_processed_seeds, package (deep-copied and repaired)
    """
    package = copy.deepcopy(package) if isinstance(package, dict) else {}
    if ordered_seeds is None:
        ordered_seeds = get_ordered_seed_list(market)
    false_processed = find_processed_zero_variant_seeds(package, ordered_seeds=ordered_seeds)
    if not false_processed:
        return {"ok": True, "repaired_count": 0, "repaired_seed_ids": [], "false_processed_seeds": [], "package": package}

    false_ids = {r["seed_id"] for r in false_processed}
    batch_state = copy.deepcopy(package.get("batch_state") if isinstance(package.get("batch_state"), dict) else {})
    _ensure_zero_variant_fields(batch_state)

    # Remove false-processed seeds from processed_seed_ids
    batch_state["processed_seed_ids"] = [sid for sid in batch_state.get("processed_seed_ids", []) if sid not in false_ids]

    # Track them explicitly
    for sid in false_ids:
        if sid not in batch_state["needs_retry_seed_ids"]:
            batch_state["needs_retry_seed_ids"].append(sid)
        if sid not in batch_state["false_processed_seed_ids"]:
            batch_state["false_processed_seed_ids"].append(sid)

    # Recompute frontier fields
    ordered_seed_ids = [s["seed_id"] for s in ordered_seeds]
    processed_set = set(batch_state["processed_seed_ids"])
    next_sid = next_unprocessed_seed_id(ordered_seed_ids, processed_set)
    batch_state["next_seed_id"] = next_sid
    if next_sid and next_sid in ordered_seed_ids:
        idx = ordered_seed_ids.index(next_sid) - 1
        batch_state["last_completed_seed_id"] = ordered_seed_ids[idx] if idx >= 0 else None
    elif not next_sid:
        batch_state["last_completed_seed_id"] = ordered_seed_ids[-1] if ordered_seed_ids else None

    package["batch_state"] = batch_state
    return {
        "ok": True,
        "repaired_count": len(false_ids),
        "repaired_seed_ids": sorted(false_ids),
        "false_processed_seeds": false_processed,
        "package": package,
    }


def evaluate_continue_guard(market: str = "IL") -> dict:
    issues: list[str] = []
    ordered = get_ordered_seed_list(market)
    local_canonical = load_local_canonical_resume_package()
    local_canonical_exists = isinstance(local_canonical, dict) and isinstance(local_canonical.get("batch_state"), dict)

    # Volatile repair fields that must be preserved from local batch_state
    _VOLATILE_FIELDS = [
        "seed_accounting", "needs_retry_seed_ids", "failed_seed_ids", "failed_details",
        "false_processed_seed_ids", "zero_variant_seed_ids", "no_variants_by_seed",
        "dedupe_proof_by_seed", "_last_queue_seed_ids", "_last_total_attempts",
    ]

    if not isinstance(local_canonical, dict):
        issues.append("canonical package missing")
        canonical_variants_count = 0
        state = normalize_batch_state_for_resume(load_batch_state(market), ordered, market=market)
    else:
        canonical_variants_count = canonical_variant_count(local_canonical)
        canonical_state = extract_canonical_batch_state(local_canonical, ordered, market=market)
        # Merge volatile repair fields from local batch_state so that repair attempts
        # (needs_retry, failed_after_retries, seed_accounting) are never overwritten.
        local_bs = load_batch_state(market)
        _ensure_zero_variant_fields(canonical_state)
        for field in _VOLATILE_FIELDS:
            local_val = local_bs.get(field)
            canonical_val = canonical_state.get(field)
            if local_val is None:
                continue
            if isinstance(local_val, dict) and isinstance(canonical_val, dict):
                # Merge: local takes precedence for seed-level accounting
                merged = dict(canonical_val)
                merged.update(local_val)
                canonical_state[field] = merged
            elif isinstance(local_val, list) and isinstance(canonical_val, list):
                # Union lists preserving order
                seen = set(canonical_val)
                extras = [v for v in local_val if v not in seen]
                canonical_state[field] = list(canonical_val) + extras
            elif field.startswith("_last_") and local_val is not None:
                # Preserve local stall-detection counters
                canonical_state[field] = local_val
        state = canonical_state
        _save_state(state)
    batch_state_exists = _batch_state_path().exists()
    processed = list(state.get("processed_seed_ids") or [])
    next_seed_id = state.get("next_seed_id")
    if not batch_state_exists and not local_canonical_exists:
        issues.append("batch_state missing")
    if canonical_variants_count == 0:
        issues.append("variants_found == 0")
    if len(processed) == 0:
        issues.append("processed_seed_ids_found == 0")
    if not next_seed_id and len(processed) < len(ordered):
        issues.append("next_seed_id is null while not all seeds are processed")
    if next_seed_id and next_seed_id in set(processed):
        issues.append("next_seed_id is already in processed_seed_ids")
    coverage = audit_coverage_until_last_completed(ordered, state, _load_outputs())
    if int(coverage.get("holes_count", 0) or 0) > 0:
        issues.append("holes exist before last_completed_seed_id")
    github = fetch_file_from_github(get_github_config().get("canonical_path") or CANONICAL_RESUME_PATH_DEFAULT)
    github_count = canonical_variant_count(github if isinstance(github, dict) else {})
    local_count = canonical_variants_count
    if github_count > 0 and local_count > 0 and local_count < github_count:
        issues.append("canonical variant count is smaller than last known GitHub/local canonical")
        issues.append("GitHub canonical is newer/larger and local import would overwrite it without merge")

    # Zero-variant false-processed seed audit — must run before allowing forward batch progress
    false_processed: list[dict] = []
    if isinstance(local_canonical, dict):
        false_processed = find_processed_zero_variant_seeds(local_canonical, ordered_seeds=ordered)
        if false_processed:
            # Only promote to a blocking issue when the canonical already carries variants that
            # include 'make' data.  Packages whose variants lack make/model fields (e.g. legacy
            # or test fixtures) must not be blocked by this heuristic — they are handled by the
            # caller via repair_required=True.
            canonical_variants = _extract_canonical_variant_bucket(local_canonical)
            variants_have_make = any(str(v.get("make") or "").strip() for v in canonical_variants)
            if variants_have_make:
                issues.append(
                    f"false_processed_zero_variant_seeds_found: {len(false_processed)} seeds have no variants, "
                    "no dedupe_proof, and no no_variants_reason"
                )

    repair_required = len(false_processed) > 0
    return {
        "passed": len(issues) == 0,
        "issues": issues,
        "canonical_variant_count": local_count,
        "github_canonical_variant_count": github_count,
        "processed_seed_count": len(processed),
        "total_seed_count": len(ordered),
        "last_completed_seed_id": state.get("last_completed_seed_id"),
        "next_seed_id": next_seed_id,
        "coverage_audit": coverage,
        "repair_required": repair_required,
        "false_processed_seed_count": len(false_processed),
        "false_processed_seeds": false_processed,
    }


def evaluate_batch_50_guard(market: str = "IL", auto_push_canonical: bool = False, auto_push_per_seed: bool = False) -> dict:
    issues: list[str] = []
    continue_guard = evaluate_continue_guard(market=market)
    issues.extend(list(continue_guard.get("issues") or []))
    ordered = get_ordered_seed_list(market)
    local_canonical = load_local_canonical_resume_package()
    local_exists = isinstance(local_canonical, dict)
    if not local_exists:
        issues.append("canonical package missing")
        state = normalize_batch_state_for_resume(load_batch_state(market), ordered, market=market)
        canonical_count = 0
    else:
        state = extract_canonical_batch_state(local_canonical, ordered, market=market)
        canonical_count = canonical_variant_count(local_canonical)
    processed = list(state.get("processed_seed_ids") or [])
    next_seed_id = state.get("next_seed_id")
    if canonical_count <= 0:
        issues.append("canonical variants count must be > 0")
    if len(processed) <= 0:
        issues.append("processed_seed_ids count must be > 0")
    if not next_seed_id:
        issues.append("next_seed_id missing")
    elif next_seed_id in set(processed):
        issues.append("next_seed_id is already in processed_seed_ids")
    coverage = continue_guard.get("coverage_audit") if isinstance(continue_guard.get("coverage_audit"), dict) else audit_coverage_until_last_completed(ordered, state, _load_outputs())
    if int(coverage.get("holes_count", 0) or 0) != 0:
        issues.append("coverage audit holes_count must be 0")
    integrity = canonical_integrity_report(market=market)
    sync_status = str(integrity.get("sync_status") or "unknown")
    if sync_status not in {"in_sync", "pending_push"}:
        issues.append(f"GitHub/local canonical sync status is '{sync_status}' (expected in_sync or pending_push)")
    previous_known = bool(isinstance(local_canonical, dict) and isinstance(local_canonical.get("merge_metadata"), dict) and local_canonical.get("merge_metadata", {}).get("previous_canonical_variants") is not None)
    if not previous_known:
        issues.append("previous canonical count is unknown")
    if not (auto_push_per_seed or auto_push_canonical or local_exists):
        issues.append("no safe persistence path: enable auto-save/push or provide local canonical")
    return {
        "passed": len(issues) == 0,
        "issues": sorted(set(issues)),
        "next_seed_id": next_seed_id,
        "processed_seed_count": len(processed),
        "canonical_variant_count": canonical_count,
        "coverage_audit": coverage,
        "sync_status": sync_status,
    }

def run_next_batch(
    limit=5,
    market="IL",
    make_filter=None,
    force_refresh=False,
    use_cache=True,
    resume=True,
    include_failed=False,
    progress_callback: Callable | None = None,
    auto_push_canonical: bool = False,
    auto_push_per_seed: bool = False,
    commit_message_prefix: str = "Update canonical vehicle variants",
):
    guard = evaluate_continue_guard(market=market)
    if int(limit or 0) == 50:
        batch_50_guard = evaluate_batch_50_guard(market=market, auto_push_canonical=auto_push_canonical, auto_push_per_seed=auto_push_per_seed)
        if not batch_50_guard.get("passed", False):
            return {"status": "blocked", "error": "Batch 50 blocked by canonical guard.", "guard": batch_50_guard, "results": []}
    # When repair_required, auto-switch to repair queue instead of blocking.
    # Only block when the guard failed for reasons unrelated to zero-variant repair.
    if not guard.get("repair_required") and not guard.get("passed", False):
        return {
            "status": "blocked",
            "error": "Batch start blocked by canonical/batch_state guard.",
            "guard": guard,
            "results": [],
        }
    ordered = get_ordered_seed_list(market)
    if resume:
        _raw_state = load_batch_state(market)
        _raw_last_completed = _raw_state.get("last_completed_seed_id")
        state = normalize_batch_state_for_resume(_raw_state, ordered, market=market)
        # Preserve stall-detection fields that are stripped out by normalization
        state["_last_queue_seed_ids"] = _raw_state.get("_last_queue_seed_ids") or []
        state["_last_total_attempts"] = _raw_state.get("_last_total_attempts", -1)
        first_seed = ordered[0]["seed_id"] if ordered else None
        if len(state.get("processed_seed_ids", [])) == 0 and not state.get("last_completed_seed_id") and not _raw_last_completed and (state.get("next_seed_id") in {None, first_seed}):
            local_canonical = load_local_canonical_resume_package()
            if isinstance(local_canonical, dict) and isinstance(local_canonical.get("batch_state"), dict):
                canonical_state = extract_canonical_batch_state(local_canonical, ordered, market=market)
                if len(canonical_state.get("processed_seed_ids", [])) > 0:
                    # Preserve stall-detection fields across the canonical override
                    _prev_last_queue = state.get("_last_queue_seed_ids", [])
                    _prev_last_attempts = state.get("_last_total_attempts", -1)
                    state = canonical_state
                    state["_last_queue_seed_ids"] = _prev_last_queue
                    state["_last_total_attempts"] = _prev_last_attempts
                    _save_state(state)
    else:
        state = _default_state(market, ordered)
    _ensure_zero_variant_fields(state)
    outputs = _load_outputs()
    if state.get("in_progress_seed_id"):
        state.setdefault("failed_details", []).append({"seed_id": state["in_progress_seed_id"], "reason": "Previous run interrupted before completion", "created_at": _now()})
        state["in_progress_seed_id"] = None
    candidates = [s for s in ordered if not make_filter or s["make"].lower() == make_filter.lower()]
    # --- Build queue ---
    if guard.get("repair_required"):
        # Auto-switch to zero-variant repair queue: pull false-processed seeds out of
        # processed_seed_ids so they go through a real processing attempt.
        false_processed_ids = {fp["seed_id"] for fp in guard.get("false_processed_seeds", [])}
        state["processed_seed_ids"] = [s for s in state.get("processed_seed_ids", []) if s not in false_processed_ids]
        for fid in false_processed_ids:
            if fid not in state.get("false_processed_seed_ids", []):
                state.setdefault("false_processed_seed_ids", []).append(fid)
            if fid not in state.get("needs_retry_seed_ids", []):
                state.setdefault("needs_retry_seed_ids", []).append(fid)
            # Remove from failed_seed_ids so they're eligible for retry
            if fid in state.get("failed_seed_ids", []):
                state["failed_seed_ids"].remove(fid)
        repair_ordered = [s for s in ordered if s["seed_id"] in false_processed_ids]
        holes = []
        coverage = audit_coverage_until_last_completed(candidates, state, outputs)
        queue = [seed_to_dict(s, default_market=market) for s in repair_ordered]
        batch_mode = "zero_variant_repair"
    else:
        coverage = audit_coverage_until_last_completed(candidates, state, outputs)
        holes = [seed_to_dict(s, default_market=market) for s in coverage["missing_seeds"]]
        processed_set = set(state.get("processed_seed_ids", []))
        next_seed_id = state.get("next_seed_id")
        forward_queue = [seed_to_dict(s, default_market=market) for s in candidates if s["seed_id"] not in processed_set and (include_failed or s["seed_id"] not in state.get("failed_seed_ids", []))]
        if next_seed_id and next_seed_id in [s.get("seed_id") for s in forward_queue]:
            idx = [s.get("seed_id") for s in forward_queue].index(next_seed_id)
            forward_queue = forward_queue[idx:]
        use_hole_repair = bool(holes) and not (next_seed_id and next_seed_id not in processed_set)
        batch_mode = "fill_coverage_holes" if use_hole_repair else "resume_forward"
        queue = holes if use_hole_repair else forward_queue
        if not queue and ordered and len(state.get("processed_seed_ids", [])) < len(ordered):
            next_id = state.get("next_seed_id") or next_unprocessed_seed_id([s["seed_id"] for s in ordered], set(state.get("processed_seed_ids", [])))
            if next_id:
                forced = next((s for s in ordered if s["seed_id"] == next_id), None)
                if forced is not None:
                    queue = [seed_to_dict(forced, default_market=market)]
    if not queue:
        _refresh_coverage(state, ordered)
        _save_state(state)
        return {"status": "completed_all", "batch_mode": "completed_all", "processed": 0, "remaining": 0, "holes_detected": bool(holes), "holes_count_before": len(holes), "holes_processed_this_batch": 0, "coverage_audit_after_batch": coverage}
    # --- Queue diagnostics ---
    current_queue_ids = [s.get("seed_id") for s in queue[:limit]]
    queue_diagnostics = {
        "queue_source": batch_mode,
        "queue_seed_ids": current_queue_ids,
        "queue_size": len(queue),
        "first_seed": current_queue_ids[0] if current_queue_ids else None,
        "last_seed": current_queue_ids[-1] if current_queue_ids else None,
        "repair_required": guard.get("repair_required", False),
        "false_processed_count": guard.get("false_processed_seed_count", 0),
    }
    # --- Stall detection ---
    current_total_attempts = _total_accounting_attempts(state)
    last_queue_ids = list(state.get("_last_queue_seed_ids") or [])
    _raw_last_attempts = state.get("_last_total_attempts")
    last_total_attempts = -1 if _raw_last_attempts is None else int(_raw_last_attempts)
    if current_queue_ids and current_queue_ids == last_queue_ids and current_total_attempts <= last_total_attempts:
        return {
            "status": "stall_detected",
            "error": "Stalled repair loop detected: same queue selected without processing attempts.",
            "queue_diagnostics": queue_diagnostics,
            "last_queue_seed_ids": last_queue_ids,
            "current_total_attempts": current_total_attempts,
            "results": [],
        }
    # Persist queue snapshot for stall detection on next run
    state["_last_queue_seed_ids"] = current_queue_ids
    state["_last_total_attempts"] = current_total_attempts
    batch_id = str(uuid.uuid4())
    state["last_batch_id"] = batch_id
    results, per_seed_canonical, execution_trace = _process_seeds(
        queue, state, ordered, limit, force_refresh, use_cache, progress_callback,
        auto_push_per_seed=auto_push_per_seed,
        commit_message_prefix=commit_message_prefix,
        market=market,
        batch_mode=batch_mode,
    )
    outputs_after = _load_outputs()
    coverage_after = audit_coverage_until_last_completed(candidates, state, outputs_after)
    remaining = len(queue) - min(limit, len(queue))
    latest_batch_path = project_root() / "data/output/latest_batch_result.json"
    payload = {"batch": {"batch_id": batch_id, "started_at": _now(), "requested_limit": limit, "processed": len(results), "batch_mode": batch_mode}, "results": results, "coverage_audit_after_batch": coverage_after}
    save_json(latest_batch_path, payload)
    canonical_result = None
    if len(results) > 0:
        canonical_result = persist_canonical_resume_package(batch_id=batch_id, push_to_github=auto_push_canonical, market=market)
    return {"status": "completed", "batch_id": batch_id, "batch_mode": batch_mode, "processed": len(results), "remaining": max(remaining, 0), "results": results, "holes_detected": bool(holes), "holes_count_before": len(holes), "holes_processed_this_batch": len(results) if holes else 0, "coverage_audit_after_batch": coverage_after, "canonical_persist": canonical_result, "per_seed_canonical": per_seed_canonical, "queue_diagnostics": queue_diagnostics, "batch_execution_trace": execution_trace}


def repair_coverage_until_clean(limit_per_pass=20, max_passes=10, market="IL"):
    passes = []
    for _ in range(max_passes):
        state = load_batch_state(market)
        ordered = get_ordered_seed_list(market)
        audit = audit_coverage_until_last_completed(ordered, state, _load_outputs())
        if audit["holes_count"] == 0:
            break
        passes.append(run_next_batch(limit=limit_per_pass, market=market, resume=True))
    return {"passes": passes, "final_audit": audit_coverage_until_last_completed(get_ordered_seed_list(market), load_batch_state(market), _load_outputs())}


def get_batch_progress(market="IL") -> dict:
    ordered = get_ordered_seed_list(market)
    local_canonical = load_local_canonical_resume_package()
    if isinstance(local_canonical, dict) and isinstance(local_canonical.get("batch_state"), dict):
        state = extract_canonical_batch_state(local_canonical, ordered, market=market)
        _save_state(state)
    else:
        state = normalize_batch_state_for_resume(load_batch_state(market), ordered, market=market)
    _refresh_coverage(state, ordered)
    audit = audit_coverage_until_last_completed(ordered, state, _load_outputs())
    next_seed = next((seed_to_dict(s) for s in ordered if s["seed_id"] == state.get("next_seed_id")), None)
    total = len(ordered); processed = len(state.get("processed_seed_ids", [])); failed = len(state.get("failed_seed_ids", []))
    coverage_rows = sorted([{"make": m, **c, "remaining": max(c.get("total", 0)-c.get("processed", 0),0)} for m,c in state.get("coverage_by_make", {}).items()], key=lambda r:r["make"].lower())
    return {"total_seeds": total, "processed": processed, "remaining": max(total-processed, 0), "failed": failed, "percent_complete": round((processed/total)*100, 1) if total else 0.0, "current_make": (next_seed or {}).get("make"), "next_seed": next_seed, "coverage_by_make": coverage_rows, "coverage_audit": audit}


def build_final_export(include_partial=True, include_verified=True, include_conflicts=False, include_unresolved=False, merge_trim_options=True, strict_no_mock=True) -> dict:
    p = get_output_paths()
    loaded = load_all_accumulated_variants()
    verified = loaded["verified"] if include_verified else []
    partial = loaded["partial"] if include_partial else []
    final_export = build_clean_final_export(
        verified_variants=verified,
        partial_variants=partial,
        sources=loaded["sources"],
        conflicts=load_json_list(p["vehicle_conflicts"]),
        unresolved=load_json_list(p["unresolved_models"]),
        include_partial=include_partial,
        include_verified=include_verified,
        include_conflicts=include_conflicts,
        include_unresolved=include_unresolved,
        merge_trim_options=merge_trim_options,
        strict_no_mock=strict_no_mock,
    )
    final_export.setdefault("audit", {})["inputs_loaded"] = loaded["inputs_loaded"]
    previous_count = max(
        int(loaded["inputs_loaded"].get("canonical_resume_package", 0) or 0),
        int(loaded["inputs_loaded"].get("imported_accumulated_dataset", 0) or 0),
    )
    final_export["audit"]["accumulation_counts"] = {
        "canonical_resume_package": int(loaded["inputs_loaded"].get("canonical_resume_package", 0) or 0),
        "canonical_source": loaded["inputs_loaded"].get("canonical_source", "missing"),
        "imported_accumulated_dataset": int(loaded["inputs_loaded"].get("imported_accumulated_dataset", 0) or 0),
        "verified_output": int(loaded["inputs_loaded"].get("vehicle_variants_verified", 0) or 0),
        "partial_output": int(loaded["inputs_loaded"].get("vehicle_variants_partial", 0) or 0),
        "latest_batch_full_variants": int(loaded["inputs_loaded"].get("latest_batch_full_variants", 0) or 0),
        "final_merged_variants": len(final_export.get("variants", [])),
        "shrink_guard_previous_count": previous_count,
        "shrink_guard_new_count": len(final_export.get("variants", [])),
    }
    assert_no_mock_in_final_export(final_export)
    return final_export


def _duplicate_variant_ids(variants: list[dict]) -> list[str]:
    seen = set()
    dup = set()
    for v in variants or []:
        if not isinstance(v, dict):
            continue
        vid = str(v.get("variant_id") or "").strip()
        if not vid:
            continue
        if vid in seen:
            dup.add(vid)
            continue
        seen.add(vid)
    return sorted(dup)


def _seed_index(seed_id: str | None, ordered_seed_ids: list[str]) -> int:
    if not seed_id:
        return -1
    try:
        return ordered_seed_ids.index(seed_id)
    except ValueError:
        return -1


def next_unprocessed_seed_id(ordered_seed_ids: list[str], processed_seed_ids: set[str]) -> str | None:
    return next((sid for sid in ordered_seed_ids if sid not in processed_seed_ids), None)


def _contains_mock_payload(variants: list[dict]) -> bool:
    for v in variants or []:
        if is_mock_contaminated_variant(v):
            return True
    return False


def validate_canonical_update(previous_package: dict | None, candidate_package: dict | None, market: str = "IL") -> dict:
    previous = previous_package if isinstance(previous_package, dict) else {}
    candidate = candidate_package if isinstance(candidate_package, dict) else {}
    issues: list[str] = []
    ordered_seed_ids = [s["seed_id"] for s in get_ordered_seed_list(market)]

    candidate_acc = candidate.get("accumulated_clean_export") if isinstance(candidate.get("accumulated_clean_export"), dict) else {}
    previous_acc = previous.get("accumulated_clean_export") if isinstance(previous.get("accumulated_clean_export"), dict) else {}
    candidate_state = candidate.get("batch_state") if isinstance(candidate.get("batch_state"), dict) else {}
    previous_state = previous.get("batch_state") if isinstance(previous.get("batch_state"), dict) else {}

    candidate_has_acc_variants = isinstance(candidate_acc.get("variants"), list) if isinstance(candidate_acc, dict) else False
    candidate_has_processed = isinstance(candidate_state.get("processed_seed_ids"), list) if isinstance(candidate_state, dict) else False
    if not candidate_has_acc_variants:
        issues.append("candidate package missing accumulated_clean_export.variants")
    if not candidate_has_processed:
        issues.append("candidate package missing batch_state.processed_seed_ids")

    candidate_variants = [v for v in (candidate_acc.get("variants") or []) if isinstance(v, dict)] if candidate_has_acc_variants else _extract_resume_variants(candidate)
    previous_variants = [v for v in (previous_acc.get("variants") or []) if isinstance(v, dict)] if isinstance(previous_acc, dict) and isinstance(previous_acc.get("variants"), list) else _extract_resume_variants(previous)
    candidate_processed = list(candidate_state.get("processed_seed_ids") or []) if candidate_has_processed else []
    previous_processed = list(previous_state.get("processed_seed_ids") or []) if isinstance(previous_state, dict) else []
    candidate_last_completed = candidate_state.get("last_completed_seed_id") if isinstance(candidate_state, dict) else None
    previous_last_completed = previous_state.get("last_completed_seed_id") if isinstance(previous_state, dict) else None
    candidate_next_seed = candidate_state.get("next_seed_id") if isinstance(candidate_state, dict) else None
    previous_next_seed = previous_state.get("next_seed_id") if isinstance(previous_state, dict) else None
    candidate_variant_count = len(candidate_variants)
    previous_variant_count = len(previous_variants)
    candidate_processed_count = len(candidate_processed)
    previous_processed_count = len(previous_processed)
    next_seed_is_processed = bool(candidate_next_seed and candidate_next_seed in set(candidate_processed))
    candidate_last_idx = _seed_index(candidate_last_completed, ordered_seed_ids)
    previous_last_idx = _seed_index(previous_last_completed, ordered_seed_ids)
    last_completed_moved_backward = bool(candidate_last_idx >= 0 and previous_last_idx >= 0 and candidate_last_idx < previous_last_idx)
    duplicate_variant_ids = _duplicate_variant_ids(candidate_variants)
    mock_hits = sorted(
        {
            str(v.get("variant_id") or "").strip()
            for v in candidate_variants
            if is_mock_contaminated_variant(v)
        }
    )
    if not mock_hits and _contains_mock_payload(candidate_variants):
        mock_hits = ["<non_id_mock_variant>"]
    quality_gate = candidate_acc.get("quality_gate", {}) if isinstance(candidate_acc, dict) else {}
    quality_gate_passed = not (isinstance(quality_gate, dict) and quality_gate and not quality_gate.get("passed", False))

    if candidate_variant_count < previous_variant_count:
        issues.append("candidate_variant_count < previous_variant_count")
    if candidate_processed_count < previous_processed_count:
        issues.append("candidate_processed_count < previous_processed_count")
    if next_seed_is_processed:
        issues.append("candidate_next_seed_id is already processed")
    if last_completed_moved_backward:
        issues.append("candidate_last_completed_seed_id moved backward")
    if duplicate_variant_ids:
        issues.append("duplicate variant_id found")
    if mock_hits:
        issues.append("mock contamination found")
    if not quality_gate_passed:
        issues.append("quality gate failed")

    candidate_audit = candidate_acc.get("audit", {}) if isinstance(candidate_acc, dict) else {}
    accumulation_counts = candidate_audit.get("accumulation_counts", {}) if isinstance(candidate_audit, dict) else {}
    final_merged_count = int(accumulation_counts.get("final_merged_variants", candidate_variant_count) or candidate_variant_count)
    latest_batch_full_variants = int(accumulation_counts.get("latest_batch_full_variants", 0) or 0)
    canonical_count = previous_variant_count
    batch_state_advanced = (
        candidate_processed_count > previous_processed_count
        or candidate_last_completed != previous_last_completed
        or candidate_next_seed != previous_next_seed
    )
    if (
        final_merged_count > canonical_count
        and latest_batch_full_variants > 0
        and not batch_state_advanced
        and str(candidate.get("_candidate_source") or "") != CANDIDATE_SOURCE_MERGED
    ):
        issues.append("candidate package has final_merged_count > canonical_count but batch_state did not advance")

    return {
        "passed": len(issues) == 0,
        "issues": issues,
        "previous_variant_count": previous_variant_count,
        "candidate_variant_count": candidate_variant_count,
        "previous_processed_count": previous_processed_count,
        "candidate_processed_count": candidate_processed_count,
        "previous_last_completed_seed_id": previous_last_completed,
        "candidate_last_completed_seed_id": candidate_last_completed,
        "previous_next_seed_id": previous_next_seed,
        "candidate_next_seed_id": candidate_next_seed,
        "duplicate_variant_ids": duplicate_variant_ids,
        "mock_hits": mock_hits,
        "quality_gate_passed": quality_gate_passed,
        "next_seed_is_processed": next_seed_is_processed,
        "last_completed_moved_backward": last_completed_moved_backward,
        "candidate_source": candidate.get("_candidate_source"),
    }


def validate_canonical_resume_package_update(new_package: dict, previous_package: dict | None = None, market: str = "IL") -> list[str]:
    return list(validate_canonical_update(previous_package, new_package, market=market).get("issues") or [])


def _compute_variant_metrics(variants: list[dict]) -> dict:
    makes = {str(v.get("make", "")).strip().lower() for v in variants if isinstance(v, dict) and v.get("make")}
    models = {
        f"{str(v.get('make', '')).strip().lower()}::{str(v.get('model', '')).strip().lower()}"
        for v in variants
        if isinstance(v, dict) and v.get("make") and v.get("model")
    }
    verified_count = sum(1 for v in variants if isinstance(v, dict) and _is_verified_variant(v))
    partial_count = max(0, len(variants) - verified_count)
    return {
        "total_variants": len(variants),
        "verified": int(verified_count),
        "partial": int(partial_count),
        "makes_count": len(makes),
        "models_count": len(models),
    }


def build_canonical_candidate(
    previous_package: dict | None,
    merged_variants: list[dict] | None,
    new_batch_state: dict | None = None,
    source: str = CANDIDATE_SOURCE_MERGED,
    market: str = "IL",
) -> dict:
    previous = previous_package if isinstance(previous_package, dict) else {}
    candidate = copy.deepcopy(previous) if isinstance(previous, dict) else {}
    candidate.setdefault("schema_version", "resume_package_v1")
    candidate["created_at"] = _now()

    variants = [copy.deepcopy(v) for v in (merged_variants or []) if isinstance(v, dict)]
    previous_state_raw = previous.get("batch_state") if isinstance(previous.get("batch_state"), dict) else {}
    previous_market = str(previous_state_raw.get("market") or "").strip()
    new_market = str((new_batch_state or {}).get("market") or "").strip() if isinstance(new_batch_state, dict) else ""
    market = previous_market or new_market or market
    ordered_seeds = get_ordered_seed_list(market)
    ordered_seed_ids = [s["seed_id"] for s in ordered_seeds]

    previous_normalized = normalize_batch_state_for_resume(previous_state_raw, ordered_seeds, variants=variants, market=market)
    previous_processed = list(previous_normalized.get("processed_seed_ids") or [])
    previous_processed_count = len(previous_processed)
    previous_next_seed = previous_normalized.get("next_seed_id")

    selected_state = previous_normalized
    if isinstance(new_batch_state, dict):
        normalized_new = normalize_batch_state_for_resume(new_batch_state, ordered_seeds, variants=variants, market=market)
        new_processed = list(normalized_new.get("processed_seed_ids") or [])
        new_processed_count = len(new_processed)
        new_next_seed = normalized_new.get("next_seed_id")
        new_processed_set = set(new_processed)
        new_next_is_processed = bool(new_next_seed and new_next_seed in new_processed_set)

        next_moved_backward = False
        allow_backward_from_coverage_holes = False
        prev_next_idx = _seed_index(previous_next_seed, ordered_seed_ids)
        new_next_idx = _seed_index(new_next_seed, ordered_seed_ids)
        if prev_next_idx >= 0 and new_next_idx >= 0 and new_next_idx < prev_next_idx:
            next_moved_backward = True
            # Backward movement is allowed only when coverage holes exist before the current frontier.
            coverage = audit_coverage_until_last_completed(ordered_seeds, normalized_new, _load_outputs())
            allow_backward_from_coverage_holes = int(coverage.get("holes_count", 0) or 0) > 0

        new_state_valid = (
            new_processed_count >= previous_processed_count
            and not new_next_is_processed
            and (not next_moved_backward or allow_backward_from_coverage_holes)
        )
        if new_state_valid:
            selected_state = normalized_new

    # Defensive fallback: never drop to an empty processed set when a previous canonical already had progress.
    if previous_processed_count > 0 and len(selected_state.get("processed_seed_ids") or []) == 0:
        selected_state = previous_normalized

    selected_state = copy.deepcopy(selected_state)
    selected_processed = list(selected_state.get("processed_seed_ids") or [])
    selected_set = set(selected_processed)
    selected_next = selected_state.get("next_seed_id")
    if selected_next in selected_set:
        resolved_next = next_unprocessed_seed_id(ordered_seed_ids, selected_set)
        if resolved_next is None and len(selected_set) < len(ordered_seed_ids):
            resolved_next = previous_next_seed
        selected_state["next_seed_id"] = resolved_next

    candidate_acc = candidate.get("accumulated_clean_export")
    if not isinstance(candidate_acc, dict):
        candidate_acc = {}
        candidate["accumulated_clean_export"] = candidate_acc
    candidate_acc["variants"] = variants

    counts = candidate.get("counts") if isinstance(candidate.get("counts"), dict) else {}
    summary = _compute_variant_metrics(variants)
    counts["total_variants"] = summary["total_variants"]
    counts["verified"] = summary["verified"]
    counts["partial"] = summary["partial"]
    counts["makes_count"] = summary["makes_count"]
    counts["models_count"] = summary["models_count"]
    candidate["counts"] = counts
    candidate["batch_state"] = selected_state
    candidate["_candidate_source"] = source
    return candidate


def build_resume_package() -> dict:
    p = get_output_paths()
    accumulated_clean_export = build_final_export()
    variants = accumulated_clean_export.get("variants", [])
    shrink = ((accumulated_clean_export.get("audit") or {}).get("accumulation_counts") or {})
    previous_count = int(shrink.get("shrink_guard_previous_count", 0) or 0)
    new_count = int(shrink.get("shrink_guard_new_count", len(variants)) or 0)
    if previous_count > 0 and new_count < previous_count:
        raise ValueError("Accumulated export shrink detected. Refusing to generate resume package.")
    makes = {str(v.get("make", "")).strip().lower() for v in variants if isinstance(v, dict) and v.get("make")}
    models = {f"{str(v.get('make','')).strip().lower()}::{str(v.get('model','')).strip().lower()}" for v in variants if isinstance(v, dict) and v.get("make") and v.get("model")}
    normalized_state = normalize_batch_state_for_resume(load_batch_state(), get_ordered_seed_list("IL"), variants=variants, market="IL")
    counts = accumulated_clean_export.get("counts", {}) if isinstance(accumulated_clean_export.get("counts"), dict) else {}
    final_package = {
        "schema_version": "resume_package_v1",
        "created_at": _now(),
        "batch_state": normalized_state,
        "run_history": load_json_list(p["run_history"]),
        "verified_variants": load_json_list(p["vehicle_variants_verified"]),
        "partial_variants": load_json_list(p["vehicle_variants_partial"]),
        "sources": load_json_list(p["vehicle_sources"]),
        "unresolved": load_json_list(p["unresolved_models"]),
        "conflicts": load_json_list(p["vehicle_conflicts"]),
        "accumulated_clean_export": accumulated_clean_export,
        "counts": {
            "total_variants": len(variants),
            "verified": int(counts.get("verified", 0) or 0),
            "partial": int(counts.get("partial", 0) or 0),
            "conflict": int(counts.get("conflict", 0) or 0),
            "unresolved": int(counts.get("unresolved", 0) or 0),
            "makes_count": len(makes),
            "models_count": len(models),
            "duplicates_removed": int(counts.get("duplicates_removed", 0) or 0),
            "mock_removed": int(counts.get("mock_removed", 0) or 0),
            "variants_with_empty_source_ids": int(counts.get("variants_with_empty_source_ids", 0) or 0),
            "variants_with_no_sources": int(counts.get("variants_with_no_sources", 0) or 0),
        },
        "merge_metadata": {
            "previous_canonical_variants": previous_count,
            "new_batch_variants": int(shrink.get("latest_batch_full_variants", 0) or 0),
            "final_variants": len(variants),
            "new_unique_added": max(0, len(variants) - previous_count),
            "dedupe_removed": int(counts.get("duplicates_removed", 0) or 0),
            "canonical_source": shrink.get("canonical_source") or ((accumulated_clean_export.get("audit", {}).get("inputs_loaded", {}) if isinstance(accumulated_clean_export.get("audit"), dict) else {}).get("canonical_source", "unknown")),
            "pushed_to_github": False,
        },
    }
    return final_package


def rebuild_canonical_metadata_from_accumulated(package: dict, seeds: list[dict]) -> dict:
    """Rebuild batch_state.coverage_by_make and top-level verified/partial lists from
    accumulated_clean_export.variants.

    accumulated_clean_export.variants is the single source of truth for variant counts.
    run_history is NOT used here — it may be incomplete and must not drive canonical coverage.

    Returns a deep copy of the package with corrected metadata so the caller's object
    is never mutated (consistent with the existing build_canonical_candidate pattern).
    """
    package = copy.deepcopy(package)
    acc = package.get("accumulated_clean_export") if isinstance(package.get("accumulated_clean_export"), dict) else {}
    variants = [v for v in (acc.get("variants") or []) if isinstance(v, dict)]

    # Build top-level verified / partial lists
    verified_variants = [v for v in variants if _is_verified_variant(v)]
    partial_variants = [v for v in variants if not _is_verified_variant(v)]
    package["verified_variants"] = verified_variants
    package["partial_variants"] = partial_variants

    # Build coverage_by_make — start from seed totals
    coverage = _empty_coverage_by_make(seeds)
    by_seed = {s["seed_id"]: s for s in seeds if isinstance(s, dict)}

    batch_state = package.get("batch_state") if isinstance(package.get("batch_state"), dict) else {}
    processed_seed_ids = set(batch_state.get("processed_seed_ids") or [])
    failed_seed_ids = set(batch_state.get("failed_seed_ids") or [])

    for sid in processed_seed_ids:
        if sid in by_seed:
            coverage[by_seed[sid]["make"]]["processed"] += 1

    for sid in failed_seed_ids:
        if sid in by_seed:
            coverage[by_seed[sid]["make"]]["failed"] += 1

    for v in variants:
        make = str(v.get("make") or "").strip()
        if make in coverage:
            if _is_verified_variant(v):
                coverage[make]["verified_variants"] += 1
            else:
                coverage[make]["partial_variants"] += 1

    for make, c in coverage.items():
        c["completed"] = c["processed"] >= c["total"] and c["total"] > 0

    # Detect and warn on any mismatch with the old coverage
    old_coverage = batch_state.get("coverage_by_make") if isinstance(batch_state.get("coverage_by_make"), dict) else {}
    mismatched_makes = []
    for make, c in coverage.items():
        old_c = old_coverage.get(make) if isinstance(old_coverage, dict) else None
        if isinstance(old_c, dict):
            if old_c.get("verified_variants", 0) != c["verified_variants"] or old_c.get("partial_variants", 0) != c["partial_variants"]:
                mismatched_makes.append(make)
    if mismatched_makes:
        print(f"[canonical] coverage_by_make repaired from accumulated_clean_export.variants for makes: {mismatched_makes}")
        package["_metadata_repaired_from_accumulated"] = True
    else:
        package.pop("_metadata_repaired_from_accumulated", None)

    if isinstance(package.get("batch_state"), dict):
        package["batch_state"]["coverage_by_make"] = coverage

    return package


def _validate_canonical_coverage_sync(package: dict) -> list[str]:
    """Return a list of warning strings if coverage_by_make is out of sync with
    accumulated_clean_export.variants.  An empty list means everything is consistent."""
    warnings_out: list[str] = []
    acc = package.get("accumulated_clean_export") if isinstance(package.get("accumulated_clean_export"), dict) else {}
    variants = [v for v in (acc.get("variants") or []) if isinstance(v, dict)]

    batch_state = package.get("batch_state") if isinstance(package.get("batch_state"), dict) else {}
    coverage = batch_state.get("coverage_by_make") if isinstance(batch_state.get("coverage_by_make"), dict) else {}

    make_verified: dict[str, int] = {}
    make_partial: dict[str, int] = {}
    for v in variants:
        make = str(v.get("make") or "").strip()
        if _is_verified_variant(v):
            make_verified[make] = make_verified.get(make, 0) + 1
        else:
            make_partial[make] = make_partial.get(make, 0) + 1

    mismatched: list[str] = []
    for make, c in coverage.items():
        exp_v = make_verified.get(make, 0)
        exp_p = make_partial.get(make, 0)
        if c.get("verified_variants", 0) != exp_v or c.get("partial_variants", 0) != exp_p:
            mismatched.append(make)

    if mismatched:
        warnings_out.append(f"coverage_by_make mismatch for makes: {sorted(mismatched)}")

    top_verified = [v for v in (package.get("verified_variants") or []) if isinstance(v, dict)]
    top_partial = [v for v in (package.get("partial_variants") or []) if isinstance(v, dict)]
    acc_verified = [v for v in variants if _is_verified_variant(v)]
    acc_partial = [v for v in variants if not _is_verified_variant(v)]
    if len(top_verified) != len(acc_verified) or len(top_partial) != len(acc_partial):
        warnings_out.append(
            f"top-level verified/partial out of sync with accumulated: "
            f"accumulated={len(acc_verified)}v/{len(acc_partial)}p, "
            f"top-level={len(top_verified)}v/{len(top_partial)}p"
        )

    return warnings_out


def _validate_saved_canonical(path) -> dict:
    """Re-open the saved canonical file and verify internal consistency.

    Returns {ok, issues} — issues is a list of human-readable problem descriptions.
    """
    from pathlib import Path as _Path
    issues: list[str] = []
    try:
        raw = _Path(path).read_text(encoding="utf-8") if _Path(path).exists() else None
        if raw is None:
            return {"ok": False, "issues": ["saved canonical file does not exist"]}
        saved = json.loads(raw)
    except Exception as exc:
        return {"ok": False, "issues": [f"failed to re-read saved canonical: {exc}"]}

    if not isinstance(saved, dict):
        return {"ok": False, "issues": ["saved canonical is not a JSON object"]}

    acc = saved.get("accumulated_clean_export") if isinstance(saved.get("accumulated_clean_export"), dict) else {}
    variants = [v for v in (acc.get("variants") or []) if isinstance(v, dict)]
    acc_verified = [v for v in variants if _is_verified_variant(v)]
    acc_partial = [v for v in variants if not _is_verified_variant(v)]

    top_verified = [v for v in (saved.get("verified_variants") or []) if isinstance(v, dict)]
    top_partial = [v for v in (saved.get("partial_variants") or []) if isinstance(v, dict)]

    # a. top-level lists must match accumulated
    if len(top_verified) != len(acc_verified) or len(top_partial) != len(acc_partial):
        issues.append(
            f"top-level verified/partial out of sync after save: "
            f"accumulated={len(acc_verified)}v/{len(acc_partial)}p, "
            f"top-level={len(top_verified)}v/{len(top_partial)}p"
        )

    batch_state = saved.get("batch_state") if isinstance(saved.get("batch_state"), dict) else {}
    coverage = batch_state.get("coverage_by_make") if isinstance(batch_state.get("coverage_by_make"), dict) else {}

    # b. coverage sums must match accumulated totals
    sum_cov_verified = sum(c.get("verified_variants", 0) for c in coverage.values() if isinstance(c, dict))
    sum_cov_partial = sum(c.get("partial_variants", 0) for c in coverage.values() if isinstance(c, dict))
    if sum_cov_verified != len(acc_verified) or sum_cov_partial != len(acc_partial):
        issues.append(
            f"coverage_by_make sums mismatch after save: "
            f"sums={sum_cov_verified}v/{sum_cov_partial}p, "
            f"accumulated={len(acc_verified)}v/{len(acc_partial)}p"
        )

    # c. BMW must not show zero if BMW variants exist
    bmw_variants = [v for v in variants if str(v.get("make") or "").strip().lower() == "bmw"]
    if bmw_variants:
        bmw_cov = coverage.get("BMW") or {}
        if bmw_cov.get("verified_variants", 0) == 0 and bmw_cov.get("partial_variants", 0) == 0:
            issues.append("BMW variants exist in accumulated but coverage_by_make BMW shows zero verified/partial")

    if issues:
        print(f"[canonical] post-save validation found {len(issues)} issue(s): {issues}")

    return {"ok": len(issues) == 0, "issues": issues}


def persist_canonical_resume_package(batch_id: str | None = None, push_to_github: bool = False, market: str = "IL") -> dict:
    previous_local = load_local_canonical_resume_package()
    previous_github = fetch_file_from_github(get_github_config().get("canonical_path") or CANONICAL_RESUME_PATH_DEFAULT)
    previous = previous_local if isinstance(previous_local, dict) else previous_github
    final_export = build_final_export()
    merged_variants = [v for v in (final_export.get("variants") or []) if isinstance(v, dict)] if isinstance(final_export, dict) else []
    package = build_canonical_candidate(
        previous,
        merged_variants,
        new_batch_state=load_batch_state(market),
        source=CANDIDATE_SOURCE_MERGED,
    )
    package_acc = package.get("accumulated_clean_export") if isinstance(package.get("accumulated_clean_export"), dict) else {}
    package_acc["quality_gate"] = (final_export.get("quality_gate") if isinstance(final_export, dict) else None)
    package_acc["audit"] = (final_export.get("audit") if isinstance(final_export, dict) else None)
    package["accumulated_clean_export"] = package_acc
    package.setdefault("merge_metadata", {})
    package["merge_metadata"].setdefault(
        "previous_canonical_variants",
        len(_extract_resume_variants(previous if isinstance(previous, dict) else {})),
    )
    package["merge_metadata"]["final_variants"] = len(merged_variants)
    package["merge_metadata"]["new_unique_added"] = max(
        0,
        len(merged_variants) - int(package["merge_metadata"].get("previous_canonical_variants", 0) or 0),
    )
    package["merge_metadata"].setdefault("pushed_to_github", False)
    # Rebuild coverage metadata from accumulated_clean_export.variants (source of truth).
    package = rebuild_canonical_metadata_from_accumulated(package, get_ordered_seed_list(market))
    validate_result = validate_canonical_update(previous, package, market=market)
    issues = list(validate_result.get("issues") or [])
    if issues:
        _set_last_canonical_update_attempt(failed=True, validate_result=validate_result, candidate_source=CANDIDATE_SOURCE_MERGED)
        return {
            "ok": False,
            "error": "Canonical resume package update blocked: shrink or invalid state detected.",
            "issues": issues,
            "validate_result": validate_result,
            "package": package,
        }
    if isinstance(previous, dict):
        save_local_canonical_backup(previous)
    save_local_canonical_resume_package(package)
    post_save = _validate_saved_canonical(_canonical_resume_path())
    if not post_save.get("ok"):
        print(f"[canonical] post-save validation warnings: {post_save.get('issues')}")
    pushed = None
    if push_to_github:
        pushed = push_canonical_resume_package(package, previous_package=previous, batch_id=batch_id)
        if not pushed.get("ok"):
            _set_last_canonical_update_attempt(failed=True, validate_result=validate_result, candidate_source=CANDIDATE_SOURCE_MERGED)
            return {
                "ok": False,
                "error": pushed.get("error") or "Failed to push canonical package to GitHub.",
                "issues": [],
                "validate_result": validate_result,
                "package": package,
                "push_result": pushed,
            }
    package.setdefault("merge_metadata", {})
    package["merge_metadata"]["pushed_to_github"] = bool(push_to_github and pushed and pushed.get("ok"))
    if pushed and pushed.get("ok"):
        package["merge_metadata"]["last_push_commit_sha"] = ((pushed.get("canonical") or {}).get("commit_sha"))
    if push_to_github and pushed and pushed.get("ok"):
        save_local_canonical_resume_package(package)
    _set_last_canonical_update_attempt(failed=False, validate_result=validate_result, candidate_source=CANDIDATE_SOURCE_MERGED)
    return {
        "ok": True,
        "issues": [],
        "validate_result": validate_result,
        "package": package,
        "push_result": pushed,
        "commit_sha": ((pushed or {}).get("canonical") or {}).get("commit_sha"),
        "post_save_validation": post_save,
    }


def _persist_batch_state_into_canonical(batch_state: dict, market: str = "IL") -> None:
    """Persist seed_accounting / failed_seed_ids / needs_retry_seed_ids into the local canonical
    package without adding or removing variants.  Used when a seed reaches failed_after_retries
    so that the repair state survives a canonical reload.
    """
    local_canonical = load_local_canonical_resume_package()
    if not isinstance(local_canonical, dict):
        return
    pkg = copy.deepcopy(local_canonical)
    canonical_bs = pkg.get("batch_state") if isinstance(pkg.get("batch_state"), dict) else {}
    _MERGE_FIELDS = [
        "seed_accounting", "needs_retry_seed_ids", "failed_seed_ids", "failed_details",
        "false_processed_seed_ids", "zero_variant_seed_ids", "no_variants_by_seed",
        "dedupe_proof_by_seed",
    ]
    for field in _MERGE_FIELDS:
        incoming = batch_state.get(field)
        existing = canonical_bs.get(field)
        if incoming is None:
            continue
        if isinstance(incoming, dict) and isinstance(existing, dict):
            merged = dict(existing)
            merged.update(incoming)
            canonical_bs[field] = merged
        elif isinstance(incoming, list) and isinstance(existing, list):
            seen = set(existing)
            canonical_bs[field] = list(existing) + [v for v in incoming if v not in seen]
        else:
            canonical_bs[field] = incoming
    pkg["batch_state"] = canonical_bs
    save_local_canonical_resume_package(pkg)


def persist_canonical_after_seed(
    seed: dict,
    batch_state: dict,
    push_to_github: bool = False,
    commit_message_prefix: str = "Update canonical vehicle variants",
    market: str = "IL",
) -> dict:
    """Persist canonical package after a single successfully completed seed.

    Local save is always performed.  GitHub push is attempted only when
    push_to_github=True.  If the GitHub push fails the local save is NOT
    rolled back — ok=True is returned together with github_push_failed=True
    and the push error details.
    """
    make = str(seed.get("make") or "").strip()
    model = str(seed.get("model") or "").strip()
    commit_msg = (
        f"{commit_message_prefix}: {make} {model} completed"
        if (make and model)
        else commit_message_prefix
    )

    previous_local = load_local_canonical_resume_package()
    previous_github = fetch_file_from_github(get_github_config().get("canonical_path") or CANONICAL_RESUME_PATH_DEFAULT)
    previous = previous_local if isinstance(previous_local, dict) else previous_github

    final_export = build_final_export()
    merged_variants = [v for v in (final_export.get("variants") or []) if isinstance(v, dict)] if isinstance(final_export, dict) else []

    package = build_canonical_candidate(
        previous,
        merged_variants,
        new_batch_state=batch_state if isinstance(batch_state, dict) else None,
        source=CANDIDATE_SOURCE_MERGED,
    )
    package_acc = package.get("accumulated_clean_export") if isinstance(package.get("accumulated_clean_export"), dict) else {}
    package_acc["quality_gate"] = final_export.get("quality_gate") if isinstance(final_export, dict) else None
    package_acc["audit"] = final_export.get("audit") if isinstance(final_export, dict) else None
    package["accumulated_clean_export"] = package_acc
    package.setdefault("merge_metadata", {}).setdefault(
        "previous_canonical_variants",
        len(_extract_resume_variants(previous if isinstance(previous, dict) else {})),
    )
    package["merge_metadata"]["final_variants"] = len(merged_variants)
    package["merge_metadata"]["new_unique_added"] = max(
        0,
        len(merged_variants) - int(package["merge_metadata"].get("previous_canonical_variants", 0) or 0),
    )
    package["merge_metadata"].setdefault("pushed_to_github", False)

    package = rebuild_canonical_metadata_from_accumulated(package, get_ordered_seed_list(market))

    validate_result = validate_canonical_update(previous, package, market=market)
    issues = list(validate_result.get("issues") or [])
    if issues:
        _set_last_canonical_update_attempt(failed=True, validate_result=validate_result, candidate_source=CANDIDATE_SOURCE_MERGED)
        return {
            "ok": False,
            "local_saved": False,
            "github_push_failed": False,
            "issues": issues,
            "validate_result": validate_result,
        }

    if isinstance(previous, dict):
        save_local_canonical_backup(previous)
    save_local_canonical_resume_package(package)
    post_save = _validate_saved_canonical(_canonical_resume_path())
    if not post_save.get("ok"):
        print(f"[canonical] per-seed post-save validation warnings: {post_save.get('issues')}")

    push_result = None
    push_error = None
    if push_to_github:
        push_result = push_canonical_resume_package(package, previous_package=previous, commit_message=commit_msg)
        if not push_result.get("ok"):
            push_error = push_result.get("error") or "Failed to push canonical to GitHub."
            _set_last_canonical_update_attempt(failed=True, validate_result=validate_result, candidate_source=CANDIDATE_SOURCE_MERGED)
            # Local save already succeeded — do NOT roll back.
            return {
                "ok": True,
                "local_saved": True,
                "github_push_failed": True,
                "push_error": push_error,
                "push_result": push_result,
                "validate_result": validate_result,
                "post_save_validation": post_save,
            }

    if push_to_github and push_result and push_result.get("ok"):
        package.setdefault("merge_metadata", {})["pushed_to_github"] = True
        package["merge_metadata"]["last_push_commit_sha"] = ((push_result.get("canonical") or {}).get("commit_sha"))
        save_local_canonical_resume_package(package)

    _set_last_canonical_update_attempt(failed=False, validate_result=validate_result, candidate_source=CANDIDATE_SOURCE_MERGED)
    return {
        "ok": True,
        "local_saved": True,
        "github_push_failed": False,
        "push_result": push_result,
        "validate_result": validate_result,
        "post_save_validation": post_save,
        "commit_sha": ((push_result or {}).get("canonical") or {}).get("commit_sha"),
    }


def push_local_canonical_to_github(batch_id: str | None = None, market: str = "IL") -> dict:
    local_package = load_local_canonical_resume_package()
    if not isinstance(local_package, dict):
        return {"ok": False, "error": "Local canonical resume package is missing or invalid JSON."}
    local_fields = _extract_package_fields(local_package)
    if int(local_fields.get("variant_count", 0) or 0) < EXPECTED_LOCAL_MIN_VARIANTS:
        return {
            "ok": False,
            "error": f"Local canonical must contain at least {EXPECTED_LOCAL_MIN_VARIANTS} variants before GitHub push.",
            "package": local_package,
        }
    previous_github = fetch_file_from_github(get_github_config().get("canonical_path") or CANONICAL_RESUME_PATH_DEFAULT)
    candidate = {
        "schema_version": local_package.get("schema_version"),
        "batch_state": copy.deepcopy(local_package.get("batch_state") if isinstance(local_package.get("batch_state"), dict) else {}),
        "accumulated_clean_export": {
            "variants": _extract_resume_variants(local_package),
            "quality_gate": ((local_package.get("accumulated_clean_export") or {}).get("quality_gate") if isinstance(local_package.get("accumulated_clean_export"), dict) else None),
            "audit": ((local_package.get("accumulated_clean_export") or {}).get("audit") if isinstance(local_package.get("accumulated_clean_export"), dict) else None),
        },
        "_candidate_source": "local_canonical",
    }
    validate_result = validate_canonical_update(previous_github if isinstance(previous_github, dict) else {}, candidate, market=market)
    if not validate_result.get("passed", False):
        _set_last_canonical_update_attempt(failed=True, validate_result=validate_result, candidate_source="local_canonical")
        return {
            "ok": False,
            "error": "Canonical resume package update blocked: shrink or invalid state detected.",
            "issues": list(validate_result.get("issues") or []),
            "validate_result": validate_result,
            "package": local_package,
        }
    pushed = push_canonical_resume_package(local_package, previous_package=previous_github, batch_id=batch_id)
    if not pushed.get("ok"):
        _set_last_canonical_update_attempt(failed=True, validate_result=validate_result, candidate_source="local_canonical")
        return {
            "ok": False,
            "error": pushed.get("error") or "Failed to push local canonical package to GitHub.",
            "package": local_package,
            "validate_result": validate_result,
            "push_result": pushed,
        }
    local_package.setdefault("merge_metadata", {})
    local_package["merge_metadata"]["pushed_to_github"] = True
    local_package["merge_metadata"]["last_push_commit_sha"] = ((pushed.get("canonical") or {}).get("commit_sha"))
    save_local_canonical_resume_package(local_package)
    _set_last_canonical_update_attempt(failed=False, validate_result=validate_result, candidate_source="local_canonical")
    return {
        "ok": True,
        "issues": [],
        "validate_result": validate_result,
        "package": local_package,
        "push_result": pushed,
        "commit_sha": ((pushed.get("canonical") or {}).get("commit_sha")),
    }


def pull_canonical_from_github() -> dict:
    cfg = get_github_config()
    payload = fetch_file_from_github(cfg.get("canonical_path") or CANONICAL_RESUME_PATH_DEFAULT)
    if not isinstance(payload, dict):
        return {"ok": False, "error": "Canonical resume package not found on GitHub."}
    ordered = get_ordered_seed_list("IL")
    normalized_state = extract_canonical_batch_state(payload, ordered, market="IL")
    payload = copy.deepcopy(payload)
    payload["batch_state"] = normalized_state
    previous = load_local_canonical_resume_package()
    if isinstance(previous, dict):
        save_local_canonical_backup(previous)
    save_local_canonical_resume_package(payload)
    save_json(_batch_state_path(), normalized_state)
    merged = dedupe_variants_stable([*load_imported_accumulated_variants(), *_extract_resume_variants(payload)])
    save_json(project_root() / "data/output/imported_accumulated_dataset.json", {"created_at": _now(), "variants": merged})
    # Run zero-variant false-processed seed audit on the pulled package
    false_processed = find_processed_zero_variant_seeds(payload, ordered_seeds=ordered)
    repair_required = len(false_processed) > 0
    return {
        "ok": True,
        "variants": canonical_variant_count(payload),
        "repair_required": repair_required,
        "false_processed_seed_count": len(false_processed),
        "false_processed_seeds": false_processed,
    }


def canonical_integrity_report(market: str = "IL") -> dict:
    local = load_local_canonical_resume_package() or {}
    github = fetch_file_from_github(get_github_config().get("canonical_path") or CANONICAL_RESUME_PATH_DEFAULT) or {}
    imported = load_imported_accumulated_variants()
    try:
        final_export = build_final_export()
    except Exception:
        final_export = {"variants": [], "quality_gate": {"passed": False}}
    local_variants = _extract_resume_variants(local)
    github_variants = _extract_resume_variants(github)
    final_variants = [v for v in final_export.get("variants", []) if isinstance(v, dict)]
    local_state = extract_canonical_batch_state(local, get_ordered_seed_list(market), market=market)
    candidate = {"accumulated_clean_export": {"variants": final_variants, "quality_gate": final_export.get("quality_gate"), "audit": final_export.get("audit")}, "batch_state": local_state, "_candidate_source": "build_final_export"}
    validate_result = validate_canonical_update(local, candidate, market=market)
    guards = list(validate_result.get("issues") or [])
    local_count = len(local_variants)
    github_count = len(github_variants)
    final_count = len(final_variants)
    if guards:
        sync_status = "invalid_candidate"
    elif local_count == github_count == final_count:
        sync_status = "in_sync"
    elif final_count > github_count:
        sync_status = "pending_push"
    elif final_count < github_count:
        sync_status = "shrink_blocked"
    else:
        sync_status = "unknown"

    return {
        "local_canonical_count": len(local_variants),
        "github_canonical_count": canonical_variant_count(github),
        "current_imported_count": len(imported),
        "final_merged_count": len(final_variants),
        "previous_processed_count": len(local.get("batch_state", {}).get("processed_seed_ids", []) if isinstance(local.get("batch_state"), dict) else []),
        "new_processed_count": len(local_state.get("processed_seed_ids", [])),
        "last_completed_seed_id": local_state.get("last_completed_seed_id"),
        "next_seed_id": local_state.get("next_seed_id"),
        "sync_status": sync_status,
        "last_push_commit_sha": ((local.get("merge_metadata") or {}).get("last_push_commit_sha") if isinstance(local.get("merge_metadata"), dict) else None),
        "shrink_guard_status": "blocked" if guards else "pass",
        "guard_issues": guards,
        "validate_result": validate_result,
    }


def rebuild_batch_state_from_outputs(market="IL") -> dict:
    ordered = get_ordered_seed_list(market); state = _default_state(market, ordered)
    outputs = _load_outputs()
    for run in outputs["run_history"]:
        sid = run.get("seed_id") or build_seed_id(run.get("make"), run.get("model"), run.get("year_start") or 0, run.get("year_end") or 0, run.get("market") or market)
        if is_seed_completed(sid, outputs, state) and sid not in state["processed_seed_ids"]:
            state["processed_seed_ids"].append(sid)
        if run.get("status") == "error" and sid not in state["failed_seed_ids"]:
            state["failed_seed_ids"].append(sid)
    remaining = [s for s in ordered if s["seed_id"] not in state["processed_seed_ids"]]
    state["next_seed_id"] = remaining[0]["seed_id"] if remaining else None
    _refresh_coverage(state, ordered); _save_state(state); return state


def cleanup_retryable_schema_errors(market: str = "IL") -> dict:
    state = load_batch_state(market)
    failed_details = state.get("failed_details", [])
    retryable_seed_ids = set()
    kept_failed_details = []
    for detail in failed_details:
        reason = str(detail.get("reason", "")).lower()
        if any(token in reason for token in RETRYABLE_SCHEMA_ERROR_TOKENS):
            retryable_seed_ids.add(detail.get("seed_id"))
            continue
        kept_failed_details.append(detail)
    state["failed_details"] = kept_failed_details
    before_failed = set(state.get("failed_seed_ids", []))
    cleaned_ids = [sid for sid in before_failed if sid in retryable_seed_ids]
    state["failed_seed_ids"] = [sid for sid in state.get("failed_seed_ids", []) if sid not in retryable_seed_ids]
    state["processed_seed_ids"] = [sid for sid in state.get("processed_seed_ids", []) if sid not in retryable_seed_ids]
    if state.get("last_completed_seed_id") in retryable_seed_ids:
        state["last_completed_seed_id"] = None
    _refresh_coverage(state, get_ordered_seed_list(market))
    _save_state(state)
    return {"status": "ok", "cleaned_seed_ids": cleaned_ids, "cleaned_count": len(cleaned_ids)}


def detect_import_file_type(uploaded_json) -> str:
    if isinstance(uploaded_json, list):
        if uploaded_json and isinstance(uploaded_json[0], dict) and "run_id" in uploaded_json[0]:
            return "run_history"
        if uploaded_json and isinstance(uploaded_json[0], dict) and "variant_id" in uploaded_json[0]:
            return "accumulated_variants"
        return "unknown"
    if uploaded_json.get("schema_version") in {"resume_package_v1", "vehicle_variant_resume_package_v1"}:
        return "resume_package"
    if isinstance(uploaded_json.get("batch_state"), dict) and isinstance(uploaded_json.get("accumulated_clean_export"), dict) and isinstance(uploaded_json.get("accumulated_clean_export", {}).get("variants"), list):
        return "resume_package"
    if isinstance(uploaded_json.get("batch_state"), dict) and isinstance(uploaded_json.get("final_export"), dict) and isinstance(uploaded_json.get("final_export", {}).get("variants"), list):
        return "resume_package"
    if isinstance(uploaded_json.get("batch_state"), dict) and isinstance(uploaded_json.get("verified_variants"), list) and isinstance(uploaded_json.get("partial_variants"), list):
        return "resume_package"
    if uploaded_json.get("schema_version") == BATCH_STATE_SCHEMA or "processed_seed_ids" in uploaded_json:
        return "batch_state"
    if "batch" in uploaded_json and "results" in uploaded_json:
        return "latest_batch_result"
    if uploaded_json.get("schema_version") == "vehicle_variants_final_v1" or "variants" in uploaded_json:
        return "final_export"
    return "unknown"


def _normalize_imported_batch_state(imported_state: dict, market: str = "IL") -> dict:
    return normalize_batch_state_for_resume(imported_state, get_ordered_seed_list(market), market=market)


def normalize_batch_state_for_resume(batch_state: dict, ordered_seeds: list[dict], variants: list[dict] | None = None, market: str = "IL", strict_zero_variant_audit: bool = False) -> dict:
    package = {"batch_state": batch_state if isinstance(batch_state, dict) else {}}
    if isinstance(variants, list):
        package["accumulated_clean_export"] = {"variants": variants}
    return extract_canonical_batch_state(package, ordered_seeds, market=market, strict_zero_variant_audit=strict_zero_variant_audit)


def import_progress_json(uploaded_json: dict | list, overwrite: bool = False, market: str = "IL") -> dict:
    file_type = detect_import_file_type(uploaded_json if isinstance(uploaded_json, dict) else uploaded_json)
    paths = get_output_paths()
    state = load_batch_state(market)
    result = {"import_status": "completed", "file_type": file_type, "processed_added": 0, "variants_verified_added": 0, "variants_partial_added": 0, "run_history_added": 0, "warnings": []}
    if file_type == "batch_state":
        incoming = uploaded_json
        incoming_processed = set(incoming.get("processed_seed_ids", []))
        local_processed = set(state.get("processed_seed_ids", []))
        merged = incoming if overwrite or len(incoming_processed) >= len(local_processed) else state
        if not overwrite:
            merged["processed_seed_ids"] = sorted(local_processed | incoming_processed)
            merged["failed_seed_ids"] = sorted(set(state.get("failed_seed_ids", [])) | set(incoming.get("failed_seed_ids", [])))
        merged = normalize_batch_state_for_resume(merged, get_ordered_seed_list(market), market=market)
        save_json(_batch_state_path(), merged)
        result["processed_added"] = len(set(merged.get("processed_seed_ids", [])) - local_processed)
    elif file_type == "latest_batch_result":
        rows = uploaded_json.get("results", [])
        for item in rows:
            sid = item.get("seed", {}).get("seed_id")
            status = (item.get("result") or {}).get("status")
            if sid and status in {"completed", "partial"} and sid not in state["processed_seed_ids"]:
                state["processed_seed_ids"].append(sid); result["processed_added"] += 1
        _save_state(state)
    elif file_type == "run_history":
        existing = load_json_list(paths["run_history"])
        old_ids = {r.get("run_id") for r in existing}
        merged = existing + [r for r in uploaded_json if r.get("run_id") not in old_ids]
        save_json(paths["run_history"], merged)
        result["run_history_added"] = len(merged) - len(existing)
    elif file_type in {"final_export", "accumulated_variants"}:
        variants = uploaded_json if isinstance(uploaded_json, list) else uploaded_json.get("variants", [])
        existing_imported = load_imported_accumulated_variants()
        incoming = dedupe_variants_stable([v for v in variants if isinstance(v, dict)])
        if overwrite:
            merged_imported = incoming
            if len(existing_imported) > 0 and len(incoming) < len(existing_imported):
                result["warnings"].append("Destructive overwrite applied to imported accumulated dataset.")
        else:
            merged_imported = dedupe_variants_stable([*existing_imported, *incoming])
            if len(existing_imported) > 0 and len(incoming) < len(existing_imported):
                result["warnings"].append("Imported accumulated dataset merged with local accumulated variants to prevent shrink.")
        save_json(project_root() / "data/output/imported_accumulated_dataset.json", {"created_at": _now(), "variants": merged_imported})
        verified = load_json_list(paths["vehicle_variants_verified"])
        partial = load_json_list(paths["vehicle_variants_partial"])
        merged_verified = _merge_variant_lists([] if overwrite else verified, [v for v in merged_imported if _is_verified_variant(v)])
        merged_partial = _merge_variant_lists([] if overwrite else partial, [v for v in merged_imported if not _is_verified_variant(v)])
        verified_ids = {v.get("variant_id") for v in merged_verified}
        merged_partial = [v for v in merged_partial if v.get("variant_id") not in verified_ids]
        result["variants_verified_added"] = max(0, len(merged_verified) - len(verified))
        result["variants_partial_added"] = max(0, len(merged_partial) - len(partial))
        save_json(paths["vehicle_variants_verified"], merged_verified)
        save_json(paths["vehicle_variants_partial"], merged_partial)
    elif file_type == "resume_package":
        pkg = uploaded_json if isinstance(uploaded_json, dict) else {}
        schema_version = pkg.get("schema_version")
        if schema_version == "vehicle_variant_resume_package_v1" and isinstance(pkg.get("final_export"), dict):
            acc = pkg.get("final_export", {})
            imported_sources = acc.get("sources", []) if isinstance(acc.get("sources", []), list) else []
        else:
            acc = pkg.get("accumulated_clean_export", {}) if isinstance(pkg.get("accumulated_clean_export"), dict) else {}
            imported_sources = pkg.get("sources", []) if isinstance(pkg.get("sources", []), list) else []
        variants = _extract_resume_variants(pkg)
        incoming_variants = dedupe_variants_stable(variants)
        existing_imported = load_imported_accumulated_variants()
        if overwrite:
            imported_variants = incoming_variants
            if len(existing_imported) > 0 and len(incoming_variants) < len(existing_imported):
                result["warnings"].append("Destructive overwrite applied to imported accumulated dataset.")
        else:
            imported_variants = dedupe_variants_stable([*existing_imported, *incoming_variants])
            if len(existing_imported) > 0 and len(incoming_variants) < len(existing_imported):
                result["warnings"].append("Imported accumulated dataset merged with local accumulated variants to prevent shrink.")
        save_json(project_root() / "data/output/imported_accumulated_dataset.json", {"created_at": _now(), "variants": imported_variants})
        verified = load_json_list(paths["vehicle_variants_verified"])
        partial = load_json_list(paths["vehicle_variants_partial"])
        v_new, p_new = _split_variants(imported_variants)
        merged_verified = _merge_variant_lists([] if overwrite else verified, v_new)
        merged_partial = _merge_variant_lists([] if overwrite else partial, p_new)
        verified_ids = {v.get("variant_id") for v in merged_verified}
        merged_partial = [v for v in merged_partial if v.get("variant_id") not in verified_ids]
        save_json(paths["vehicle_variants_verified"], merged_verified)
        save_json(paths["vehicle_variants_partial"], merged_partial)
        if imported_sources:
            save_json(paths["vehicle_sources"], imported_sources if overwrite else (load_json_list(paths["vehicle_sources"]) + imported_sources))
        if schema_version != "vehicle_variant_resume_package_v1":
            if overwrite:
                save_json(paths["run_history"], pkg.get("run_history", []))
            else:
                save_json(paths["run_history"], load_json_list(paths["run_history"]) + pkg.get("run_history", []))
            save_json(paths["unresolved_models"], pkg.get("unresolved", []))
            save_json(paths["vehicle_conflicts"], pkg.get("conflicts", []))
        ordered = get_ordered_seed_list(market)
        imported_package = copy.deepcopy(pkg)
        if not isinstance(imported_package.get("batch_state"), dict):
            imported_package["batch_state"] = state
        normalized_state = extract_canonical_batch_state(imported_package, ordered, market=market)
        save_json(_batch_state_path(), normalized_state)
        imported_pkg = copy.deepcopy(pkg)
        imported_pkg["schema_version"] = "resume_package_v1"
        imported_pkg["batch_state"] = normalized_state
        imported_pkg["accumulated_clean_export"] = {"variants": imported_variants}
        imported_pkg["_candidate_source"] = "uploaded_resume"
        # Rebuild coverage metadata from accumulated_clean_export.variants (source of truth)
        imported_pkg = rebuild_canonical_metadata_from_accumulated(imported_pkg, ordered)
        validate_result = validate_canonical_update(load_local_canonical_resume_package(), imported_pkg, market=market)
        guard_issues = list(validate_result.get("issues") or [])
        if len(normalized_state.get("processed_seed_ids", [])) == 0:
            guard_issues.append("processed_seed_ids is empty after import")
        if guard_issues:
            result["warnings"].append("Canonical resume package update blocked: shrink or invalid state detected.")
            result["warnings"].extend(guard_issues)
            validate_result["issues"] = guard_issues
            validate_result["passed"] = False
            _set_last_canonical_update_attempt(failed=True, validate_result=validate_result, candidate_source="uploaded_resume")
        else:
            previous_local = load_local_canonical_resume_package()
            if isinstance(previous_local, dict):
                save_local_canonical_backup(previous_local)
            save_local_canonical_resume_package(imported_pkg)
            post_save = _validate_saved_canonical(_canonical_resume_path())
            if not post_save.get("ok"):
                result["warnings"].extend(post_save.get("issues") or [])
            _set_last_canonical_update_attempt(failed=False, validate_result=validate_result, candidate_source="uploaded_resume")
        result["processed_added"] = max(0, len(set(normalized_state.get("processed_seed_ids", [])) - set(state.get("processed_seed_ids", []))))
        result["variants_verified_added"] = max(0, len(merged_verified) - len(verified))
        result["variants_partial_added"] = max(0, len(merged_partial) - len(partial))
        c = acc.get("counts", {}) if isinstance(acc, dict) else {}
        result["imported_variants"] = len(imported_variants)
        result["imported_makes"] = c.get("makes_count")
        result["imported_models"] = c.get("models_count")
        verified_count = len([v for v in imported_variants if _is_verified_variant(v)])
        partial_count = max(0, len(imported_variants) - verified_count)
        next_seed_id = normalized_state.get("next_seed_id")
        next_seed = next((s for s in ordered if s.get("seed_id") == next_seed_id), None)
        next_seed_human = (
            f"{next_seed.get('make')} {next_seed.get('model')} {next_seed.get('year_start')}–{next_seed.get('year_end')}"
            if isinstance(next_seed, dict)
            else None
        )
        coverage = audit_coverage_until_last_completed(ordered, normalized_state, _load_outputs())
        guard_issues_for_continue = []
        if len(imported_variants) == 0:
            guard_issues_for_continue.append("variants_found == 0")
        if len(normalized_state.get("processed_seed_ids", [])) == 0:
            guard_issues_for_continue.append("processed_seed_ids_found == 0")
        if not next_seed_id and len(normalized_state.get("processed_seed_ids", [])) < len(ordered):
            guard_issues_for_continue.append("next_seed_id is null while seeds remain")
        if next_seed_id and next_seed_id in set(normalized_state.get("processed_seed_ids", [])):
            guard_issues_for_continue.append("next_seed_id is already in processed_seed_ids")
        if int(coverage.get("holes_count", 0) or 0) > 0:
            guard_issues_for_continue.append("holes exist before last_completed_seed_id")
        # Zero-variant false-processed seed audit on the imported package (informational).
        # Reported as repair_required but NOT added to guard_issues_for_continue so that
        # imports of packages with legacy/simplified variants (no make fields) are not blocked.
        false_processed_import = find_processed_zero_variant_seeds(imported_pkg, ordered_seeds=ordered)
        import_repair_required = len(false_processed_import) > 0
        result.update(
            {
                "detected_file_type": file_type,
                "variants_found": len(imported_variants),
                "verified_count": verified_count,
                "partial_count": partial_count,
                "processed_seed_ids_found": len(normalized_state.get("processed_seed_ids", [])),
                "total_seeds": len(ordered),
                "last_completed_seed_id": normalized_state.get("last_completed_seed_id"),
                "next_seed_id": next_seed_id,
                "next_seed_human_readable": next_seed_human,
                "coverage_audit_status": "pass" if int(coverage.get("holes_count", 0) or 0) == 0 else "blocked",
                "holes_count": int(coverage.get("holes_count", 0) or 0),
                "safe_to_continue": len(guard_issues_for_continue) == 0 and len(guard_issues) == 0,
                "continue_guard_issues": guard_issues_for_continue,
                "validate_result": validate_result,
                "repair_required": import_repair_required,
                "false_processed_seed_count": len(false_processed_import),
                "false_processed_seeds": false_processed_import,
            }
        )
    else:
        result["import_status"] = "skipped"
        result["warnings"].append("Unknown import file type")
    if file_type != "resume_package":
        rebuild_batch_state_from_outputs(market)
    else:
        audit_coverage_until_last_completed(get_ordered_seed_list(market), load_batch_state(market), _load_outputs())
    return result
