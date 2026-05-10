"""Canonical-first problem queue (single source of truth).

The previous architecture spread runtime state across multiple JSON files
under ``data/output/`` (``rerun_queue.json``, ``batch_state.json``,
``latest_batch_result.json``, ...) which fell out of sync and broke the
UI progress axis.  This module enforces the new contract:

* ``data/canonical/resume_package_canonical.json`` is the **only**
  permanent state file.  All progress derives from
  ``canonical.problem_repair_state`` and
  ``canonical.batch_state.needs_retry_seed_ids``.
* ``data/output/problem_queue.json`` is an **optional, derived** mirror
  of the canonical state.  It exists only while problem-repair work is
  active and is regenerated from canonical on every update.  When the
  queue is empty the file is deleted.
* Other ``data/output/*.json`` files (``batch_state.json``,
  ``rerun_queue.json``, ``latest_batch_result.json``, ...) must never
  decide what runs.

Vehicle closure rule (the BMW 850i regression):
    A seed is considered *closed* if **any** of:

    * ``variants_added_to_canonical > 0`` (real new variants), or
    * ``dedupe_proof`` records at least one matched variant id, or
    * ``no_variants_reason`` is one of the canonical allow-list reasons.

This means a seed that returns variants which all dedupe against
existing canonical variants still counts as completed and must move the
progress axis from ``N/T`` to ``N+1/T``.
"""
from __future__ import annotations

import copy
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from storage.json_store import (
    load_json_object,
    project_root,
    save_json,
)

CANONICAL_PATH = "data/canonical/resume_package_canonical.json"
CANONICAL_BACKUP_PATH = "data/canonical/resume_package_backup_previous.json"
CANONICAL_BACKUPS_DIR = "data/canonical/backups"
PROBLEM_QUEUE_PATH = "data/output/problem_queue.json"

DEFAULT_NORMAL_CONTINUATION_LAST = "gmc__yukon__2000__2026__il"
DEFAULT_NORMAL_CONTINUATION_NEXT = "haval__h6__2022__2026__il"

# The canonical allow-list of no_variants reasons.  Kept inline here so the
# problem-queue engine has no dependency on the archived legacy
# ``agent/rerun_queue_manager.py`` module.
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


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _root() -> Path:
    return project_root()


def canonical_path() -> Path:
    return _root() / CANONICAL_PATH


def problem_queue_path() -> Path:
    return _root() / PROBLEM_QUEUE_PATH


def backup_path() -> Path:
    return _root() / CANONICAL_BACKUP_PATH


def backups_dir() -> Path:
    return _root() / CANONICAL_BACKUPS_DIR


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# ---------------------------------------------------------------------------
# Canonical load / save with timestamped backup
# ---------------------------------------------------------------------------

def load_canonical() -> dict | None:
    payload = load_json_object(canonical_path())
    return payload if isinstance(payload, dict) and payload else None


def _ensure_dir(p: Path) -> None:
    try:
        p.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass


def write_canonical_backup_timestamped(package: dict) -> Path | None:
    """Write ``data/canonical/backups/resume_package_canonical_<ts>.json``.

    The user explicitly required that *every* canonical write creates a
    fresh timestamped backup so we never restore from a stale file.
    """
    if not isinstance(package, dict):
        return None
    d = backups_dir()
    _ensure_dir(d)
    path = d / f"resume_package_canonical_{_now_stamp()}.json"
    try:
        save_json(path, package)
        return path
    except Exception:
        return None


def save_canonical(package: dict, write_timestamped_backup: bool = True) -> None:
    """Persist canonical, refreshing both the "previous" backup and a
    timestamped backup under ``data/canonical/backups/``.
    """
    if not isinstance(package, dict):
        return
    path = canonical_path()
    _ensure_dir(path.parent)
    # Roll the existing canonical into resume_package_backup_previous.json
    # before overwriting, so the "previous" pointer is always one step
    # behind, not the stale uploaded copy.
    try:
        if path.exists():
            shutil.copyfile(path, backup_path())
    except Exception:
        pass
    save_json(path, package)
    if write_timestamped_backup:
        write_canonical_backup_timestamped(package)


# ---------------------------------------------------------------------------
# Closure / resolution rules
# ---------------------------------------------------------------------------

def _proof_is_valid(proof: Any) -> bool:
    if not isinstance(proof, dict):
        return False
    matched = proof.get("matched_variant_ids") or proof.get("matched")
    return isinstance(matched, (list, tuple)) and len(matched) > 0


def _no_variants_reason_is_valid(reason: Any) -> bool:
    if isinstance(reason, dict):
        reason = reason.get("reason")
    return isinstance(reason, str) and reason in ALLOWED_NO_VARIANTS_REASONS


def classify_seed_closure(result: dict | None) -> tuple[bool, str]:
    """Apply the canonical seed-closure rule.

    Returns ``(closed, status)`` where ``status`` is one of
    ``completed_added`` / ``completed_deduped`` / ``completed_no_variants_reason``
    / ``failed_retry``.
    """
    if not isinstance(result, dict):
        return (False, "failed_retry")
    variants_added = result.get("variants_added_to_canonical")
    if not isinstance(variants_added, int):
        # Some pipelines record the count under different field names.
        variants_added = (
            result.get("variants_added")
            or result.get("new_variant_count")
            or 0
        )
        try:
            variants_added = int(variants_added or 0)
        except Exception:
            variants_added = 0
    if variants_added > 0:
        return (True, "completed_added")
    if _proof_is_valid(result.get("dedupe_proof")):
        return (True, "completed_deduped")
    if _no_variants_reason_is_valid(result.get("no_variants_reason")):
        return (True, "completed_no_variants_reason")
    return (False, "failed_retry")


# ---------------------------------------------------------------------------
# Canonical-first progress derivation
# ---------------------------------------------------------------------------

def _bs(canonical: dict | None) -> dict:
    if isinstance(canonical, dict):
        bs = canonical.get("batch_state")
        if isinstance(bs, dict):
            return bs
    return {}


def _string_list(value: Any) -> list[str]:
    return [s for s in (value or []) if isinstance(s, str) and s]


# A well-formed seed id looks like ``<make>__<model>__<year_start>__<year_end>__<market>``
# (e.g. ``bmw__850i__2018__2026__il``).  Anything that does not match
# this shape — most notably the ``"s1"`` token observed in the uploaded
# canonical — is treated as test pollution and quarantined.
import re as _re
_SEED_ID_RE = _re.compile(r"^[a-z0-9][a-z0-9._\-+:]*__[^_].*__\d{4}__\d{4}__[a-z]{2,}$")


def _is_valid_seed_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_SEED_ID_RE.match(value))


def sanitize_problem_seed_lists(canonical: dict) -> dict:
    """Filter ``batch_state.needs_retry_seed_ids`` down to valid seed ids
    that also belong to the canonical ``false_processed`` set.

    Any rejected token is appended to
    ``batch_state.invalid_needs_retry_seed_ids`` (without duplicates) so
    diagnostics remain available, but the value never leaks back into the
    active problem queue.  Mutates ``canonical`` in place and returns it.
    """
    if not isinstance(canonical, dict):
        return canonical
    bs = canonical.setdefault("batch_state", {})

    raw_needs = list(bs.get("needs_retry_seed_ids") or [])
    fp_valid = [s for s in _string_list(bs.get("false_processed_seed_ids")) if _is_valid_seed_id(s)]
    fp_set = set(fp_valid)

    valid: list[str] = []
    invalid: list[str] = []
    seen: set[str] = set()
    for entry in raw_needs:
        if not _is_valid_seed_id(entry):
            invalid.append(entry if isinstance(entry, str) else repr(entry))
            continue
        if fp_set and entry not in fp_set:
            # Seed not present in false_processed → not a valid problem
            # repair candidate.  Quarantine it.
            invalid.append(entry)
            continue
        if entry in seen:
            continue
        seen.add(entry)
        valid.append(entry)

    bs["needs_retry_seed_ids"] = valid
    bs["false_processed_seed_ids"] = list(fp_valid)
    if invalid:
        existing_invalid = bs.get("invalid_needs_retry_seed_ids") or []
        if not isinstance(existing_invalid, list):
            existing_invalid = []
        for item in invalid:
            if item not in existing_invalid:
                existing_invalid.append(item)
        bs["invalid_needs_retry_seed_ids"] = existing_invalid
    return canonical


def compute_problem_repair_state(canonical: dict | None) -> dict:
    """Derive the canonical ``problem_repair_state`` block.

    The result is **purely** a function of canonical fields; it never
    consults ``data/output/*.json``.  This is the single function the UI,
    the runtime selector and the queue exporter must rely on.

    Implementation note: the derivation is built from the *valid*
    ``false_processed_seed_ids`` intersected with ``needs_retry_seed_ids``,
    so invalid tokens such as ``"s1"`` never appear in
    ``pending_seed_ids`` / ``total`` / ``progress`` even if they leaked
    into a polluted ``needs_retry_seed_ids`` upstream.
    """
    bs = _bs(canonical)
    prs_existing = (
        canonical.get("problem_repair_state")
        if isinstance(canonical, dict) and isinstance(canonical.get("problem_repair_state"), dict)
        else {}
    )

    # Build the authoritative valid problem-seed set from canonical.
    fp_valid = [s for s in _string_list(bs.get("false_processed_seed_ids")) if _is_valid_seed_id(s)]
    fp_set = set(fp_valid)
    needs_raw = _string_list(bs.get("needs_retry_seed_ids"))
    # Pending = valid needs_retry that is also in false_processed (when
    # false_processed is populated).  Invalid tokens are stripped here too.
    needs_retry = [
        s for s in needs_raw
        if _is_valid_seed_id(s) and (not fp_set or s in fp_set)
    ]

    completed_recorded = [
        s for s in _string_list(prs_existing.get("completed_seed_ids")) if _is_valid_seed_id(s)
    ]
    failed_recorded = [
        s for s in _string_list(prs_existing.get("failed_retry_seed_ids")) if _is_valid_seed_id(s)
    ]

    # ``problem_repair_state.original_problem_seed_ids`` is the canonical
    # source of TOTAL when present (the user-uploaded canonical contains
    # exactly the 54 problem seeds in that list).  We never recompute
    # total from len(pending) — that would shrink total to 53 after the
    # first BMW completion which is the regression we just fixed.
    original_problem = [
        s for s in _string_list(prs_existing.get("original_problem_seed_ids")) if _is_valid_seed_id(s)
    ]
    total: int
    if original_problem:
        total = len(original_problem)
    else:
        total_candidate = bs.get("original_false_processed_count")
        if isinstance(total_candidate, int) and total_candidate > 0:
            total = total_candidate
        else:
            orig = [s for s in _string_list(bs.get("false_processed_seed_ids_original")) if _is_valid_seed_id(s)]
            if orig:
                total = len(orig)
            else:
                total = len(fp_valid) or (len(completed_recorded) + len(needs_retry))

    pending = len(needs_retry)
    completed = max(total - pending, 0)
    # Prefer the recorded count when canonical has it, but never trust a
    # count larger than ``total - pending``.
    if completed_recorded:
        completed = min(max(completed, len(completed_recorded)), max(total - pending, 0))

    failed_retry = len(failed_recorded)
    current_seed_id = needs_retry[0] if needs_retry else None
    last_completed_seed_id = prs_existing.get("last_completed_seed_id")
    if not _is_valid_seed_id(last_completed_seed_id):
        last_completed_seed_id = completed_recorded[-1] if completed_recorded else None

    if total and pending:
        current_position = f"{completed + 1} / {total}"
    elif total:
        current_position = f"{total} / {total}"
    else:
        current_position = "0 / 0"
    percent = round((completed / total) * 100.0, 1) if total else 0.0

    normal = prs_existing.get("normal_continuation")
    if not isinstance(normal, dict):
        normal = {}
    # Normal continuation is FROZEN at Haval H6 while problem queue is
    # active.  We never advance these pointers from the problem queue.
    normal_last = (
        normal.get("last_completed_seed_id")
        or bs.get("last_completed_seed_id")
        or DEFAULT_NORMAL_CONTINUATION_LAST
    )
    normal_next = (
        normal.get("next_seed_id")
        or bs.get("next_seed_id")
        or DEFAULT_NORMAL_CONTINUATION_NEXT
    )

    return {
        "active": pending > 0,
        "total": int(total or 0),
        "original_problem_seed_ids": list(original_problem),
        "completed_seed_ids": list(completed_recorded),
        "pending_seed_ids": list(needs_retry),
        "failed_retry_seed_ids": list(failed_recorded),
        "last_completed_seed_id": last_completed_seed_id,
        "current_seed_id": current_seed_id,
        "normal_continuation": {
            "last_completed_seed_id": normal_last,
            "next_seed_id": normal_next,
        },
        "progress": {
            "completed": completed,
            "pending": pending,
            "failed_retry": failed_retry,
            "current_position": current_position,
            "percent": percent,
        },
    }


def compute_progress(canonical: dict | None) -> dict:
    """Lightweight, UI-friendly progress snapshot derived from canonical.

    Independent of any output file.  Matches the spec's
    "Canonical progress rules" section.
    """
    bs = _bs(canonical)
    prs = compute_problem_repair_state(canonical)
    progress = prs["progress"]
    return {
        "mode": "problem_queue" if prs["active"] else "normal_batch",
        "total_problem_seeds": prs["total"],
        "completed_problem_seeds": progress["completed"],
        "pending_problem_seeds": progress["pending"],
        "failed_retry_problem_seeds": progress["failed_retry"],
        "current_problem_seed": prs["current_seed_id"],
        "current_position": progress["current_position"],
        "last_completed_problem_seed": prs["last_completed_seed_id"],
        "percent": progress["percent"],
        "normal_continuation_last_completed_seed_id": prs["normal_continuation"]["last_completed_seed_id"],
        "normal_continuation_next_seed_id": prs["normal_continuation"]["next_seed_id"],
        "normal_next_seed_id": bs.get("next_seed_id") or prs["normal_continuation"]["next_seed_id"],
    }


# ---------------------------------------------------------------------------
# Derived ``data/output/problem_queue.json`` (mirror only)
# ---------------------------------------------------------------------------

def build_problem_queue_payload(canonical: dict | None) -> dict:
    """Build the derived queue payload.  Never persisted state — purely a
    mirror of canonical for export / dashboard tooling.
    """
    prs = compute_problem_repair_state(canonical)
    progress = prs["progress"]
    pending_ids = prs["pending_seed_ids"]
    completed_ids = prs["completed_seed_ids"]
    failed_ids = prs["failed_retry_seed_ids"]
    return {
        "schema_version": "problem_queue_v1",
        "generated_at": _now_iso(),
        "source": "canonical:problem_repair_state",
        "active": prs["active"],
        "total": prs["total"],
        "pending": progress["pending"],
        "completed": progress["completed"],
        "failed_retry": progress["failed_retry"],
        "first_pending": pending_ids[0] if pending_ids else None,
        "current_seed_id": prs["current_seed_id"],
        "last_completed_seed_id": prs["last_completed_seed_id"],
        "pending_seed_ids": list(pending_ids),
        "completed_seed_ids": list(completed_ids),
        "failed_retry_seed_ids": list(failed_ids),
        "normal_continuation": dict(prs["normal_continuation"]),
        "progress": dict(progress),
    }


def regenerate_problem_queue(
    canonical: dict | None = None,
    *,
    delete_if_complete: bool = True,
) -> dict:
    """Regenerate ``data/output/problem_queue.json`` from canonical.

    Returns the payload that was written (or would be written when
    ``canonical`` is empty).  When the queue is complete and
    ``delete_if_complete`` is True the file is removed.
    """
    if canonical is None:
        canonical = load_canonical() or {}
    payload = build_problem_queue_payload(canonical)
    path = problem_queue_path()
    _ensure_dir(path.parent)
    if delete_if_complete and not payload["active"] and payload["pending"] == 0:
        try:
            if path.exists():
                path.unlink()
        except Exception:
            pass
        return payload
    save_json(path, payload)
    return payload


def delete_problem_queue() -> bool:
    """Remove the derived ``data/output/problem_queue.json`` file."""
    path = problem_queue_path()
    try:
        if path.exists():
            path.unlink()
            return True
    except Exception:
        return False
    return False


# ---------------------------------------------------------------------------
# Canonical mutators (the *only* place that may advance problem progress)
# ---------------------------------------------------------------------------

def _ensure_problem_repair_state(canonical: dict) -> dict:
    """Refresh ``canonical['problem_repair_state']`` in place and return it."""
    # Always sanitize the underlying seed lists first so invalid tokens
    # (e.g. ``"s1"``) are quarantined into ``invalid_needs_retry_seed_ids``
    # before any progress numbers are computed.
    sanitize_problem_seed_lists(canonical)
    prs = compute_problem_repair_state(canonical)
    canonical["problem_repair_state"] = prs
    return prs


def refresh_problem_repair_state(canonical: dict | None = None, *, persist: bool = True) -> dict:
    """Recompute ``problem_repair_state`` and (optionally) persist.

    Useful as a one-shot fix when a canonical has been hand-edited or
    uploaded without the derived block.
    """
    if canonical is None:
        canonical = load_canonical() or {}
    _ensure_problem_repair_state(canonical)
    if persist:
        save_canonical(canonical)
        regenerate_problem_queue(canonical)
    return canonical["problem_repair_state"]


def mark_seed_completed(
    seed_id: str,
    *,
    result: dict | None = None,
    canonical: dict | None = None,
    persist: bool = True,
) -> dict:
    """Record a successful problem-queue seed in canonical.

    Updates ``batch_state.needs_retry_seed_ids``,
    ``batch_state.false_processed_seed_ids`` (if present),
    ``problem_repair_state.completed_seed_ids`` /
    ``last_completed_seed_id`` and the derived progress block.
    Returns the resulting ``problem_repair_state`` dict.
    """
    if not isinstance(seed_id, str) or not seed_id:
        raise ValueError("seed_id is required")
    own_load = canonical is None
    if own_load:
        canonical = load_canonical() or {}
    if not isinstance(canonical, dict):
        canonical = {}
    bs = canonical.setdefault("batch_state", {})

    # Remove from needs_retry only.  false_processed_seed_ids is intentionally
    # preserved so that compute_problem_repair_state can still derive
    # total = len(false_processed_seed_ids) = 54 after seeds are completed.
    # Removing completed seeds from false_processed caused total to shrink
    # from 54 → 53 and completed to read 0 (Bug 2 regression).
    needs = [s for s in _string_list(bs.get("needs_retry_seed_ids")) if s != seed_id]
    bs["needs_retry_seed_ids"] = needs

    prs = canonical.setdefault("problem_repair_state", {})
    completed = _string_list(prs.get("completed_seed_ids"))
    if seed_id not in completed:
        completed.append(seed_id)
    prs["completed_seed_ids"] = completed
    prs["last_completed_seed_id"] = seed_id

    # Drop from failed_retry if it had been recorded there previously.
    prs["failed_retry_seed_ids"] = [
        s for s in _string_list(prs.get("failed_retry_seed_ids")) if s != seed_id
    ]

    _, status = classify_seed_closure(result) if result is not None else (True, "completed_added")
    status_log = canonical.setdefault("problem_repair_status_log", [])
    if isinstance(status_log, list):
        status_log.append({
            "seed_id": seed_id,
            "status": status,
            "recorded_at": _now_iso(),
        })

    # Recompute derived state.
    refreshed = _ensure_problem_repair_state(canonical)

    if persist:
        save_canonical(canonical)
        regenerate_problem_queue(canonical)
    return refreshed


def mark_seed_failed_retry(
    seed_id: str,
    *,
    canonical: dict | None = None,
    persist: bool = True,
) -> dict:
    """Record a problem-queue seed as ``failed_retry`` (stays pending)."""
    if not isinstance(seed_id, str) or not seed_id:
        raise ValueError("seed_id is required")
    if canonical is None:
        canonical = load_canonical() or {}
    if not isinstance(canonical, dict):
        canonical = {}
    prs = canonical.setdefault("problem_repair_state", {})
    failed = _string_list(prs.get("failed_retry_seed_ids"))
    if seed_id not in failed:
        failed.append(seed_id)
    prs["failed_retry_seed_ids"] = failed

    refreshed = _ensure_problem_repair_state(canonical)
    if persist:
        save_canonical(canonical)
        regenerate_problem_queue(canonical)
    return refreshed


# ---------------------------------------------------------------------------
# Runtime selector
# ---------------------------------------------------------------------------

def select_next_seed(canonical: dict | None = None) -> dict:
    """Return the next seed to run, derived purely from canonical.

    Output shape::

        {"mode": "problem_queue" | "normal_batch",
         "seed_id": "<seed>",
         "blocks_normal_batch": bool}
    """
    if canonical is None:
        canonical = load_canonical() or {}
    prs = compute_problem_repair_state(canonical)
    if prs["active"]:
        return {
            "mode": "problem_queue",
            "seed_id": prs["current_seed_id"],
            "blocks_normal_batch": True,
        }
    bs = _bs(canonical)
    return {
        "mode": "normal_batch",
        "seed_id": bs.get("next_seed_id") or DEFAULT_NORMAL_CONTINUATION_NEXT,
        "blocks_normal_batch": False,
    }


# ---------------------------------------------------------------------------
# Canonical validation
# ---------------------------------------------------------------------------

def validate_canonical_state(canonical: dict | None = None) -> dict:
    """Validate the canonical state and return a structured report.

    Confirms the invariants the runtime depends on:

    * ``batch_state`` and ``problem_repair_state`` exist
    * No invalid seed ids (e.g. ``"s1"``) leaked into the active lists
    * When problem-repair is active, the selected seed is not the
      Haval H6 normal-continuation cursor
    * When problem-repair is active, the head of
      ``batch_state.needs_retry_seed_ids`` equals
      ``problem_repair_state.current_seed_id``
    """
    if canonical is None:
        canonical = load_canonical() or {}
    issues: list[str] = []
    if not isinstance(canonical, dict) or not canonical:
        return {"ok": False, "issues": ["canonical missing or empty"], "active": False}

    bs = canonical.get("batch_state") if isinstance(canonical.get("batch_state"), dict) else None
    if bs is None:
        issues.append("batch_state missing")
        bs = {}
    prs = canonical.get("problem_repair_state") if isinstance(canonical.get("problem_repair_state"), dict) else None
    if prs is None:
        issues.append("problem_repair_state missing")
        prs = {}

    needs_retry = _string_list(bs.get("needs_retry_seed_ids"))
    invalid_in_needs = [s for s in needs_retry if not _is_valid_seed_id(s) or s == "s1"]
    if invalid_in_needs:
        issues.append(f"invalid seed ids in needs_retry_seed_ids: {invalid_in_needs}")

    derived = compute_problem_repair_state(canonical)
    selection = select_next_seed(canonical)
    if derived["active"]:
        if selection["seed_id"] == DEFAULT_NORMAL_CONTINUATION_NEXT:
            issues.append("problem_queue active but selector returned Haval H6")
        head = next(iter(derived["pending_seed_ids"]), None)
        if head and selection["seed_id"] != head:
            issues.append(
                f"selector seed_id {selection['seed_id']!r} does not match needs_retry head {head!r}"
            )

    return {
        "ok": not issues,
        "issues": issues,
        "active": derived["active"],
        "mode": selection["mode"],
        "seed_id": selection["seed_id"],
        "total": derived["total"],
        "pending": derived["progress"]["pending"],
        "completed": derived["progress"]["completed"],
        "current_position": derived["progress"]["current_position"],
    }


# ---------------------------------------------------------------------------
# Partial-persist repair
# ---------------------------------------------------------------------------

def repair_problem_queue_partial_persist_state(
    canonical: dict | None = None,
    *,
    persist: bool = True,
) -> dict:
    """Detect and repair seeds that are marked completed without persisted evidence.

    A seed is considered partially-persisted when it appears in
    ``problem_repair_state.completed_seed_ids`` but neither variants for it
    exist in ``accumulated_clean_export.variants``, nor a valid ``dedupe_proof``
    exists in ``batch_state.dedupe_proof_by_seed``, nor a valid
    ``no_variants_reason`` exists in ``batch_state.no_variants_by_seed``.

    Such seeds are moved back to the front of ``needs_retry_seed_ids`` and
    removed from ``completed_seed_ids`` so they will be re-processed on the
    next run.

    Returns a report dict: ``{"ok", "repaired", "changed"}``.
    """
    own_load = canonical is None
    if own_load:
        canonical = load_canonical() or {}
    if not isinstance(canonical, dict):
        return {"ok": False, "repaired": [], "changed": False, "issue": "canonical missing"}

    bs = canonical.get("batch_state") if isinstance(canonical.get("batch_state"), dict) else {}
    prs = canonical.get("problem_repair_state") if isinstance(canonical.get("problem_repair_state"), dict) else {}

    completed = _string_list(prs.get("completed_seed_ids"))
    if not completed:
        return {"ok": True, "repaired": [], "changed": False}

    # Build evidence lookup structures.
    acc = canonical.get("accumulated_clean_export") if isinstance(canonical.get("accumulated_clean_export"), dict) else {}
    variants = acc.get("variants") if isinstance(acc.get("variants"), list) else []
    variant_makes_models = {
        (str(v.get("make") or "").strip().lower(), str(v.get("model") or "").strip().lower())
        for v in variants if isinstance(v, dict)
    }
    dedupe_proof = bs.get("dedupe_proof_by_seed") if isinstance(bs.get("dedupe_proof_by_seed"), dict) else {}
    no_variants = bs.get("no_variants_by_seed") if isinstance(bs.get("no_variants_by_seed"), dict) else {}

    repaired: list[str] = []
    for seed_id in list(completed):
        parts = seed_id.split("__")
        if len(parts) < 2:
            continue
        seed_make = parts[0].lower()
        seed_model = parts[1].lower()

        has_variants = (seed_make, seed_model) in variant_makes_models
        dp_entry = dedupe_proof.get(seed_id)
        has_dedupe = (
            isinstance(dp_entry, dict)
            and isinstance(dp_entry.get("matched_variant_ids"), list)
            and len(dp_entry.get("matched_variant_ids")) > 0
        )
        nv_entry = no_variants.get(seed_id)
        nv_reason = nv_entry.get("reason") if isinstance(nv_entry, dict) else None
        has_no_variants_reason = _no_variants_reason_is_valid(nv_reason)

        if not (has_variants or has_dedupe or has_no_variants_reason):
            repaired.append(seed_id)

    if not repaired:
        return {"ok": True, "repaired": [], "changed": False}

    # Move repaired seeds back to the front of needs_retry.
    repaired_set = set(repaired)
    bs_dict = canonical.setdefault("batch_state", {})
    existing_needs_retry = _string_list(bs_dict.get("needs_retry_seed_ids"))
    new_needs_retry = repaired + [s for s in existing_needs_retry if s not in repaired_set]
    bs_dict["needs_retry_seed_ids"] = new_needs_retry

    # Ensure repaired seeds are present in false_processed_seed_ids.
    existing_fp = _string_list(bs_dict.get("false_processed_seed_ids"))
    fp_set = set(existing_fp)
    for sid in repaired:
        if sid not in fp_set:
            existing_fp.append(sid)
            fp_set.add(sid)
    bs_dict["false_processed_seed_ids"] = existing_fp

    # Remove from completed_seed_ids.
    prs_dict = canonical.setdefault("problem_repair_state", {})
    prs_dict["completed_seed_ids"] = [s for s in completed if s not in repaired_set]
    remaining_completed = prs_dict["completed_seed_ids"]
    prs_dict["last_completed_seed_id"] = remaining_completed[-1] if remaining_completed else None

    # Recompute derived state.
    _ensure_problem_repair_state(canonical)

    if persist:
        save_canonical(canonical)
        regenerate_problem_queue(canonical)

    return {
        "ok": True,
        "repaired": repaired,
        "changed": True,
        "restored_to_pending": repaired,
    }




# ``save_canonical_atomic`` is the spec name for the atomic canonical
# write used by the engine.  Our ``save_canonical`` already writes
# atomically via ``storage.json_store.save_json`` (write-temp + rename)
# and rolls a previous-backup, so we expose it under both names.
save_canonical_atomic = save_canonical

# ``regenerate_problem_queue_mirror`` is the spec name for rebuilding
# ``data/output/problem_queue.json`` from canonical.  Existing callers
# use ``regenerate_problem_queue``.
regenerate_problem_queue_mirror = regenerate_problem_queue

# ``create_backup_from_canonical`` is the spec name for the timestamped
# backup helper.
create_backup_from_canonical = write_canonical_backup_timestamped


__all__ = [
    "ALLOWED_NO_VARIANTS_REASONS",
    "build_problem_queue_payload",
    "canonical_path",
    "classify_seed_closure",
    "compute_problem_repair_state",
    "compute_progress",
    "create_backup_from_canonical",
    "delete_problem_queue",
    "load_canonical",
    "mark_seed_completed",
    "mark_seed_failed_retry",
    "problem_queue_path",
    "refresh_problem_repair_state",
    "regenerate_problem_queue",
    "regenerate_problem_queue_mirror",
    "repair_problem_queue_partial_persist_state",
    "sanitize_problem_seed_lists",
    "save_canonical",
    "save_canonical_atomic",
    "select_next_seed",
    "validate_canonical_state",
    "write_canonical_backup_timestamped",
]
