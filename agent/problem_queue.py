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

# Reuse the canonical allow-list of no_variants reasons from the batch
# runner / rerun-queue manager so the closure rule stays consistent.
try:  # pragma: no cover - import-time fallback
    from agent.rerun_queue_manager import ALLOWED_NO_VARIANTS_REASONS
except Exception:  # pragma: no cover
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

    # Total problem seeds = original_false_processed_count OR
    # len(false_processed_seed_ids_original) per spec; fall back to the
    # cleaned ``false_processed_seed_ids`` set (then completed + pending).
    total = bs.get("original_false_processed_count")
    if not isinstance(total, int) or total <= 0:
        orig = [s for s in _string_list(bs.get("false_processed_seed_ids_original")) if _is_valid_seed_id(s)]
        total = len(orig)
    if not isinstance(total, int) or total <= 0:
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

    # Remove from needs_retry / false_processed.
    needs = [s for s in _string_list(bs.get("needs_retry_seed_ids")) if s != seed_id]
    bs["needs_retry_seed_ids"] = needs
    fp = [s for s in _string_list(bs.get("false_processed_seed_ids")) if s != seed_id]
    bs["false_processed_seed_ids"] = fp

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


__all__ = [
    "ALLOWED_NO_VARIANTS_REASONS",
    "build_problem_queue_payload",
    "canonical_path",
    "classify_seed_closure",
    "compute_problem_repair_state",
    "compute_progress",
    "delete_problem_queue",
    "load_canonical",
    "mark_seed_completed",
    "mark_seed_failed_retry",
    "problem_queue_path",
    "refresh_problem_repair_state",
    "regenerate_problem_queue",
    "sanitize_problem_seed_lists",
    "save_canonical",
    "select_next_seed",
    "write_canonical_backup_timestamped",
]
