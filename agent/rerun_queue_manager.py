"""Dedicated rerun queue manager for processed-zero-variant seeds.

This module owns ``data/output/rerun_queue.json``: a deterministic,
auditable queue that captures seeds previously marked processed in the
canonical resume package but which have zero variants and no valid
``no_variants_reason`` / ``dedupe_proof``.

Design goals (mirrors the implementation plan):

* The queue file is the **single** active repair gate.  When it exists
  with pending items, the runner must process only those seeds and is
  forbidden from advancing the normal continuation cursor.
* The queue is created either by scanning canonical state
  (:meth:`RerunQueueManager.scan_and_create_queue`) or from an explicit
  curated list (:meth:`RerunQueueManager.create_queue_from_exact_seed_list`).
* Successful reruns move seeds into ``completed``; reruns that come
  back with zero variants and no valid proof/reason stay in
  ``failed_retry`` and the normal batch remains blocked.
* When all original rerun seeds are resolved, completed seeds are
  merged back into ``batch_state.processed_seed_ids`` / ``processed_seeds``
  in canonical seed order and the queue file is removed.
"""
from __future__ import annotations

import copy
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from storage.json_store import project_root, save_json, load_json_object

SCHEMA_VERSION = "rerun_queue_v1"
DEFAULT_QUEUE_PATH = "data/output/rerun_queue.json"
DEFAULT_CANONICAL_PATH = "data/canonical/resume_package_canonical.json"
DEFAULT_REASON = "processed_zero_variant_without_proof"
DEFAULT_NORMAL_CONTINUATION = {
    "last_completed_seed_id": "gmc__yukon__2000__2026__il",
    "next_seed_id": "haval__h6__2022__2026__il",
}

# Status values used inside pending entries.
STATUS_PENDING = "pending"
STATUS_FAILED_RETRY = "failed_retry"

# Reuse the canonical allow-list of no_variants reasons.
try:  # pragma: no cover - import-time fallback
    from agent.batch_runner import ALLOWED_NO_VARIANTS_REASONS
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
# Exact curated list of 54 problematic seeds (user-provided source of truth).
# ---------------------------------------------------------------------------

EXACT_54_RERUN_SEEDS: list[dict[str, Any]] = [
    {"seed_id": "bmw__850i__2018__2026__il", "make": "BMW", "model": "850i", "year_start": 2018, "year_end": 2026},
    {"seed_id": "bmw__z4_sdrive20i__2019__2026__il", "make": "BMW", "model": "Z4 sDrive20i", "year_start": 2019, "year_end": 2026},
    {"seed_id": "daewoo__lacetti__2003__2011__il", "make": "Daewoo", "model": "Lacetti", "year_start": 2003, "year_end": 2011},
    {"seed_id": "daihatsu__copen__2002__2012__il", "make": "Daihatsu", "model": "Copen", "year_start": 2002, "year_end": 2012},
    {"seed_id": "ds_automobiles__ds_9__2020__2026__il", "make": "DS Automobiles", "model": "DS 9", "year_start": 2020, "year_end": 2026},
    {"seed_id": "fiat__bravo__1995__2014__il", "make": "Fiat", "model": "Bravo", "year_start": 1995, "year_end": 2014},
    {"seed_id": "fiat__freemont__2011__2016__il", "make": "Fiat", "model": "Freemont", "year_start": 2011, "year_end": 2016},
    {"seed_id": "fiat__linea__2007__2018__il", "make": "Fiat", "model": "Linea", "year_start": 2007, "year_end": 2018},
    {"seed_id": "fiat__tipo__2016__2026__il", "make": "Fiat", "model": "Tipo", "year_start": 2016, "year_end": 2026},
    {"seed_id": "ford__ecosport__2013__2022__il", "make": "Ford", "model": "EcoSport", "year_start": 2013, "year_end": 2022},
    {"seed_id": "geely__coolray__2023__2026__il", "make": "Geely", "model": "Coolray", "year_start": 2023, "year_end": 2026},
    {"seed_id": "geely__monjaro__2023__2026__il", "make": "Geely", "model": "Monjaro", "year_start": 2023, "year_end": 2026},
    {"seed_id": "haval__jolion__2021__2026__il", "make": "Haval", "model": "Jolion", "year_start": 2021, "year_end": 2026},
    {"seed_id": "honda__accord__1990__2024__il", "make": "Honda", "model": "Accord", "year_start": 1990, "year_end": 2024},
    {"seed_id": "honda__civic__1990__2026__il", "make": "Honda", "model": "Civic", "year_start": 1990, "year_end": 2026},
    {"seed_id": "honda__civic_type_r__2017__2023__il", "make": "Honda", "model": "Civic Type R", "year_start": 2017, "year_end": 2023},
    {"seed_id": "honda__cr-v__1997__2026__il", "make": "Honda", "model": "CR-V", "year_start": 1997, "year_end": 2026},
    {"seed_id": "honda__cr-z__2010__2016__il", "make": "Honda", "model": "CR-Z", "year_start": 2010, "year_end": 2016},
    {"seed_id": "honda__e:ny1__2023__2026__il", "make": "Honda", "model": "e:Ny1", "year_start": 2023, "year_end": 2026},
    {"seed_id": "honda__fr-v__2004__2009__il", "make": "Honda", "model": "FR-V", "year_start": 2004, "year_end": 2009},
    {"seed_id": "honda__hr-v__2015__2026__il", "make": "Honda", "model": "HR-V", "year_start": 2015, "year_end": 2026},
    {"seed_id": "honda__insight__2009__2014__il", "make": "Honda", "model": "Insight", "year_start": 2009, "year_end": 2014},
    {"seed_id": "honda__insight__2018__2022__il", "make": "Honda", "model": "Insight", "year_start": 2018, "year_end": 2022},
    {"seed_id": "honda__jazz__2001__2026__il", "make": "Honda", "model": "Jazz", "year_start": 2001, "year_end": 2026},
    {"seed_id": "honda__legend__2000__2012__il", "make": "Honda", "model": "Legend", "year_start": 2000, "year_end": 2012},
    {"seed_id": "honda__odyssey__1999__2020__il", "make": "Honda", "model": "Odyssey", "year_start": 1999, "year_end": 2020},
    {"seed_id": "honda__prelude__1992__2001__il", "make": "Honda", "model": "Prelude", "year_start": 1992, "year_end": 2001},
    {"seed_id": "honda__stream__2001__2014__il", "make": "Honda", "model": "Stream", "year_start": 2001, "year_end": 2014},
    {"seed_id": "honda__zr-v__2023__2026__il", "make": "Honda", "model": "ZR-V", "year_start": 2023, "year_end": 2026},
    {"seed_id": "hongqi__e-hs9__2022__2026__il", "make": "Hongqi", "model": "E-HS9", "year_start": 2022, "year_end": 2026},
    {"seed_id": "hummer__h2__2003__2009__il", "make": "Hummer", "model": "H2", "year_start": 2003, "year_end": 2009},
    {"seed_id": "hummer__h3__2005__2010__il", "make": "Hummer", "model": "H3", "year_start": 2005, "year_end": 2010},
    {"seed_id": "hyundai__atos__1997__2014__il", "make": "Hyundai", "model": "Atos", "year_start": 1997, "year_end": 2014},
    {"seed_id": "hyundai__bayon__2021__2026__il", "make": "Hyundai", "model": "Bayon", "year_start": 2021, "year_end": 2026},
    {"seed_id": "hyundai__casper__2022__2026__il", "make": "Hyundai", "model": "Casper", "year_start": 2022, "year_end": 2026},
    {"seed_id": "hyundai__coupe__1996__2009__il", "make": "Hyundai", "model": "Coupe", "year_start": 1996, "year_end": 2009},
    {"seed_id": "hyundai__creta__2020__2026__il", "make": "Hyundai", "model": "Creta", "year_start": 2020, "year_end": 2026},
    {"seed_id": "hyundai__elantra__1990__2026__il", "make": "Hyundai", "model": "Elantra", "year_start": 1990, "year_end": 2026},
    {"seed_id": "hyundai__excel__1990__1998__il", "make": "Hyundai", "model": "Excel", "year_start": 1990, "year_end": 1998},
    {"seed_id": "hyundai__getz__2002__2011__il", "make": "Hyundai", "model": "Getz", "year_start": 2002, "year_end": 2011},
    {"seed_id": "hyundai__grandeur__2006__2020__il", "make": "Hyundai", "model": "Grandeur", "year_start": 2006, "year_end": 2020},
    {"seed_id": "hyundai__h1__1997__2021__il", "make": "Hyundai", "model": "H1", "year_start": 1997, "year_end": 2021},
    {"seed_id": "hyundai__i10__2008__2026__il", "make": "Hyundai", "model": "i10", "year_start": 2008, "year_end": 2026},
    {"seed_id": "hyundai__i20__2009__2026__il", "make": "Hyundai", "model": "i20", "year_start": 2009, "year_end": 2026},
    {"seed_id": "hyundai__i25__1994__2026__il", "make": "Hyundai", "model": "i25", "year_start": 1994, "year_end": 2026},
    {"seed_id": "hyundai__i30__2007__2025__il", "make": "Hyundai", "model": "i30", "year_start": 2007, "year_end": 2025},
    {"seed_id": "hyundai__i40__2011__2019__il", "make": "Hyundai", "model": "i40", "year_start": 2011, "year_end": 2019},
    {"seed_id": "hyundai__ioniq_5__2021__2026__il", "make": "Hyundai", "model": "Ioniq 5", "year_start": 2021, "year_end": 2026},
    {"seed_id": "hyundai__ioniq_5_n__2024__2026__il", "make": "Hyundai", "model": "Ioniq 5 N", "year_start": 2024, "year_end": 2026},
    {"seed_id": "hyundai__ioniq_6__2023__2026__il", "make": "Hyundai", "model": "Ioniq 6", "year_start": 2023, "year_end": 2026},
    {"seed_id": "hyundai__ioniq__2016__2022__il", "make": "Hyundai", "model": "Ioniq", "year_start": 2016, "year_end": 2022},
    {"seed_id": "hyundai__ix35__2010__2015__il", "make": "Hyundai", "model": "ix35", "year_start": 2010, "year_end": 2015},
    {"seed_id": "hyundai__kona__2017__2026__il", "make": "Hyundai", "model": "Kona", "year_start": 2017, "year_end": 2026},
    {"seed_id": "infiniti__qx80__2010__2022__il", "make": "Infiniti", "model": "QX80", "year_start": 2010, "year_end": 2022},
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_path(path: str | os.PathLike[str]) -> Path:
    p = Path(path)
    if p.is_absolute():
        return p
    return project_root() / p


def _is_valid_seed_id(sid: Any) -> bool:
    """Heuristic: canonical seed_ids are strings using the
    ``make__model__year_start__year_end__market`` shape with at least
    four ``__`` separators.
    """
    if not isinstance(sid, str) or not sid:
        return False
    return sid.count("__") >= 4


def _seed_entry(seed: dict, reason: str = DEFAULT_REASON) -> dict:
    return {
        "seed_id": seed.get("seed_id"),
        "make": seed.get("make"),
        "model": seed.get("model"),
        "year_start": seed.get("year_start"),
        "year_end": seed.get("year_end"),
        "market": seed.get("market", "IL"),
        "reason": reason,
        "attempts": 0,
        "last_status": None,
    }


def _has_valid_proof(result: dict | None) -> bool:
    if not isinstance(result, dict):
        return False
    proof = result.get("dedupe_proof")
    if isinstance(proof, dict):
        matched = proof.get("matched_variant_ids") or proof.get("matched")
        if isinstance(matched, (list, tuple)) and len(matched) > 0:
            return True
    return False


def _has_valid_no_variants_reason(result: dict | None) -> bool:
    if not isinstance(result, dict):
        return False
    reason = result.get("no_variants_reason")
    if isinstance(reason, dict):
        reason = reason.get("reason")
    return isinstance(reason, str) and reason in ALLOWED_NO_VARIANTS_REASONS


# ---------------------------------------------------------------------------
# RerunQueueManager
# ---------------------------------------------------------------------------

class RerunQueueManager:
    """Owner of ``data/output/rerun_queue.json``."""

    def __init__(
        self,
        canonical_path: str | os.PathLike[str] = DEFAULT_CANONICAL_PATH,
        queue_path: str | os.PathLike[str] = DEFAULT_QUEUE_PATH,
        market: str = "IL",
    ) -> None:
        self.canonical_path = _resolve_path(canonical_path)
        self.queue_path = _resolve_path(queue_path)
        self.market = market

    # ------------------------------------------------------------------
    # Canonical helpers
    # ------------------------------------------------------------------

    def _load_canonical(self) -> dict | None:
        payload = load_json_object(self.canonical_path)
        if isinstance(payload, dict) and payload:
            return payload
        return None

    def _save_canonical(self, package: dict) -> None:
        self.canonical_path.parent.mkdir(parents=True, exist_ok=True)
        save_json(self.canonical_path, package)

    # ------------------------------------------------------------------
    # Queue file IO
    # ------------------------------------------------------------------

    def _write_queue(self, queue: dict) -> dict:
        queue = copy.deepcopy(queue)
        queue["schema_version"] = SCHEMA_VERSION
        queue.setdefault("market", self.market)
        queue.setdefault("source_canonical_path", str(self.canonical_path.relative_to(project_root())) if self._is_below_root() else str(self.canonical_path))
        queue.setdefault("reason", DEFAULT_REASON)
        queue.setdefault("normal_continuation", copy.deepcopy(DEFAULT_NORMAL_CONTINUATION))
        queue.setdefault("created_at", _now_iso())
        queue["updated_at"] = _now_iso()
        queue.setdefault("pending", [])
        queue.setdefault("completed", [])
        queue.setdefault("failed_retry", [])
        queue.setdefault("invalid_seed_ids", [])
        queue["total"] = int(queue.get("total") or (len(queue["pending"]) + len(queue["completed"]) + len(queue["failed_retry"])))
        self._refresh_progress(queue)
        self.queue_path.parent.mkdir(parents=True, exist_ok=True)
        save_json(self.queue_path, queue)
        return queue

    def _is_below_root(self) -> bool:
        try:
            self.canonical_path.relative_to(project_root())
            return True
        except Exception:
            return False

    def _refresh_progress(self, queue: dict) -> None:
        total = int(queue.get("total") or 0)
        completed = len(queue.get("completed") or [])
        pending = len(queue.get("pending") or [])
        failed = len(queue.get("failed_retry") or [])
        percent = int(round(100 * completed / total)) if total > 0 else 0
        queue["progress"] = {
            "completed": completed,
            "pending": pending,
            "failed_retry": failed,
            "percent": percent,
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def queue_exists(self) -> bool:
        return self.queue_path.exists()

    def load_queue(self) -> dict:
        if not self.queue_path.exists():
            return {}
        try:
            with open(self.queue_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def has_pending(self) -> bool:
        q = self.load_queue()
        return bool(q.get("pending"))

    def next_seed(self) -> dict | None:
        q = self.load_queue()
        pending = q.get("pending") or []
        return copy.deepcopy(pending[0]) if pending else None

    def is_complete(self) -> bool:
        q = self.load_queue()
        if not q:
            return False
        pending = q.get("pending") or []
        failed = q.get("failed_retry") or []
        return len(pending) == 0 and len(failed) == 0

    # ------------------------------------------------------------------
    # Creation
    # ------------------------------------------------------------------

    def create_queue_from_exact_seed_list(
        self,
        seeds: list[dict] | None = None,
        normal_continuation: dict | None = None,
    ) -> dict:
        """Persist a queue from a curated list (default: the user's 54).

        Also rewrites canonical so that the curated seed_ids are removed
        from ``batch_state.processed_seed_ids`` / ``processed_seeds``,
        leaving variants and accumulated_clean_export untouched.
        """
        source = seeds if seeds is not None else EXACT_54_RERUN_SEEDS
        pending: list[dict] = []
        invalid: list[str] = []
        seen: set[str] = set()
        for raw in source:
            if not isinstance(raw, dict):
                continue
            sid = raw.get("seed_id")
            if not _is_valid_seed_id(sid) or sid in seen:
                if isinstance(sid, str) and sid:
                    invalid.append(sid)
                continue
            seen.add(sid)
            entry = _seed_entry({**raw, "market": raw.get("market", self.market)})
            pending.append(entry)

        cont = copy.deepcopy(normal_continuation) if isinstance(normal_continuation, dict) else copy.deepcopy(DEFAULT_NORMAL_CONTINUATION)
        queue = {
            "schema_version": SCHEMA_VERSION,
            "created_at": _now_iso(),
            "market": self.market,
            "source_canonical_path": DEFAULT_CANONICAL_PATH,
            "reason": DEFAULT_REASON,
            "normal_continuation": cont,
            "total": len(pending),
            "pending": pending,
            "completed": [],
            "failed_retry": [],
            "invalid_seed_ids": invalid,
        }
        queue = self._write_queue(queue)
        self._strip_queue_seeds_from_canonical([p["seed_id"] for p in pending], normal_continuation=cont)
        return queue

    def scan_and_create_queue(self, ordered_seeds: list[dict] | None = None) -> dict:
        """Scan canonical for processed-zero-variant seeds and persist a queue.

        The detection algorithm reuses the canonical helper in
        ``agent.batch_runner``; this method is provided so future runs
        can refresh the queue automatically when canonical state drifts.
        """
        from agent.batch_runner import (
            find_processed_zero_variant_seeds,
            get_ordered_seed_list,
        )

        package = self._load_canonical()
        if package is None:
            queue = {
                "schema_version": SCHEMA_VERSION,
                "created_at": _now_iso(),
                "market": self.market,
                "source_canonical_path": DEFAULT_CANONICAL_PATH,
                "reason": DEFAULT_REASON,
                "normal_continuation": copy.deepcopy(DEFAULT_NORMAL_CONTINUATION),
                "total": 0,
                "pending": [],
                "completed": [],
                "failed_retry": [],
                "invalid_seed_ids": [],
            }
            return self._write_queue(queue)
        ordered = ordered_seeds or get_ordered_seed_list(self.market)
        bs = package.get("batch_state") or {}
        next_seed_id = bs.get("next_seed_id") or DEFAULT_NORMAL_CONTINUATION["next_seed_id"]
        last_completed = bs.get("last_completed_seed_id") or DEFAULT_NORMAL_CONTINUATION["last_completed_seed_id"]

        detected = find_processed_zero_variant_seeds(package, ordered)
        # Preserve canonical seed order.
        order = {s.get("seed_id"): idx for idx, s in enumerate(ordered or []) if isinstance(s, dict)}
        detected_sorted = sorted(detected, key=lambda d: order.get(d.get("seed_id"), 10_000_000))

        pending = [_seed_entry({
            "seed_id": d.get("seed_id"),
            "make": d.get("make"),
            "model": d.get("model"),
            "year_start": d.get("year_start"),
            "year_end": d.get("year_end"),
            "market": self.market,
        }) for d in detected_sorted if _is_valid_seed_id(d.get("seed_id"))]

        normal_cont = {
            "last_completed_seed_id": last_completed,
            "next_seed_id": next_seed_id,
        }

        queue = {
            "schema_version": SCHEMA_VERSION,
            "created_at": _now_iso(),
            "market": self.market,
            "source_canonical_path": DEFAULT_CANONICAL_PATH,
            "reason": DEFAULT_REASON,
            "normal_continuation": normal_cont,
            "total": len(pending),
            "pending": pending,
            "completed": [],
            "failed_retry": [],
            "invalid_seed_ids": [],
        }
        queue = self._write_queue(queue)
        self._strip_queue_seeds_from_canonical([p["seed_id"] for p in pending], normal_continuation=normal_cont)
        return queue

    # ------------------------------------------------------------------
    # Canonical mutation
    # ------------------------------------------------------------------

    def _strip_queue_seeds_from_canonical(
        self,
        seed_ids: list[str],
        normal_continuation: dict | None = None,
    ) -> None:
        """Remove queue seed_ids from canonical processed lists.

        Variants and accumulated_clean_export are left intact, as are
        any other fields.  This is a deterministic in-place rewrite.
        """
        if not seed_ids:
            return
        package = self._load_canonical()
        if package is None:
            return
        bs = package.get("batch_state")
        if not isinstance(bs, dict):
            return
        sid_set = {s for s in seed_ids if isinstance(s, str)}
        before_ids = list(bs.get("processed_seed_ids") or [])
        bs["processed_seed_ids"] = [s for s in before_ids if s not in sid_set]
        before_seeds = list(bs.get("processed_seeds") or [])
        bs["processed_seeds"] = [
            s for s in before_seeds
            if not (isinstance(s, dict) and s.get("seed_id") in sid_set)
        ]
        if isinstance(normal_continuation, dict):
            if normal_continuation.get("next_seed_id"):
                bs["next_seed_id"] = normal_continuation["next_seed_id"]
            if normal_continuation.get("last_completed_seed_id"):
                bs["last_completed_seed_id"] = normal_continuation["last_completed_seed_id"]
        self._save_canonical(package)

    # ------------------------------------------------------------------
    # Status transitions
    # ------------------------------------------------------------------

    def _pop_pending(self, queue: dict, seed_id: str) -> dict | None:
        pending = queue.get("pending") or []
        for i, entry in enumerate(pending):
            if entry.get("seed_id") == seed_id:
                return pending.pop(i)
        # Maybe currently in failed_retry — promote back to a workable copy
        failed = queue.get("failed_retry") or []
        for i, entry in enumerate(failed):
            if entry.get("seed_id") == seed_id:
                return failed.pop(i)
        return None

    def mark_success(self, seed_id: str, variants_added: int, result: dict | None = None) -> dict:
        queue = self.load_queue()
        if not queue:
            return {"ok": False, "error": "no_queue", "seed_id": seed_id}
        entry = self._pop_pending(queue, seed_id)
        if entry is None:
            return {"ok": False, "error": "seed_not_in_queue", "seed_id": seed_id}
        resolved = (int(variants_added or 0) > 0
                    or _has_valid_no_variants_reason(result)
                    or _has_valid_proof(result))
        entry["attempts"] = int(entry.get("attempts") or 0) + 1
        entry["last_status"] = "success" if resolved else "no_variants_no_proof"
        entry["variants_added"] = int(variants_added or 0)
        entry["resolved_at"] = _now_iso()
        if not resolved:
            # Refuse: caller asked for success but proof is missing.
            queue.setdefault("failed_retry", []).append(entry)
            self._write_queue(queue)
            return {"ok": False, "error": "missing_proof_or_reason", "seed_id": seed_id}
        queue.setdefault("completed", []).append(entry)
        self._write_queue(queue)
        return {"ok": True, "seed_id": seed_id, "completed_count": len(queue.get("completed") or [])}

    def mark_failed_retry(self, seed_id: str, result: dict | None = None) -> dict:
        queue = self.load_queue()
        if not queue:
            return {"ok": False, "error": "no_queue", "seed_id": seed_id}
        entry = self._pop_pending(queue, seed_id)
        if entry is None:
            return {"ok": False, "error": "seed_not_in_queue", "seed_id": seed_id}
        entry["attempts"] = int(entry.get("attempts") or 0) + 1
        entry["last_status"] = STATUS_FAILED_RETRY
        if isinstance(result, dict):
            entry["last_result_summary"] = {
                "variants_added": int(result.get("variants_added") or 0),
                "has_no_variants_reason": _has_valid_no_variants_reason(result),
                "has_dedupe_proof": _has_valid_proof(result),
            }
        queue.setdefault("failed_retry", []).append(entry)
        self._write_queue(queue)
        return {"ok": True, "seed_id": seed_id, "failed_retry_count": len(queue.get("failed_retry") or [])}

    # ------------------------------------------------------------------
    # Progress / finalize
    # ------------------------------------------------------------------

    def progress_summary(self) -> dict:
        queue = self.load_queue()
        if not queue:
            return {
                "total_rerun": 0,
                "pending_count": 0,
                "completed_count": 0,
                "failed_retry_count": 0,
                "current_seed": None,
                "normal_continuation_seed": DEFAULT_NORMAL_CONTINUATION["next_seed_id"],
                "can_run_normal_batch": True,
                "progress_percent": 0,
            }
        total = int(queue.get("total") or 0)
        pending = list(queue.get("pending") or [])
        completed = list(queue.get("completed") or [])
        failed = list(queue.get("failed_retry") or [])
        percent = int(round(100 * len(completed) / total)) if total > 0 else 0
        normal = queue.get("normal_continuation") or {}
        return {
            "total_rerun": total,
            "pending_count": len(pending),
            "completed_count": len(completed),
            "failed_retry_count": len(failed),
            "current_seed": pending[0].get("seed_id") if pending else None,
            "current_position": (len(completed) + 1) if pending else (len(completed) or total),
            "normal_continuation_seed": normal.get("next_seed_id"),
            "can_run_normal_batch": len(pending) == 0 and len(failed) == 0,
            "progress_percent": percent,
        }

    def finalize_if_complete(self) -> dict:
        """Merge completed seeds back into canonical and delete the queue.

        Only succeeds when there are zero pending and zero failed_retry
        entries.  Otherwise returns ``{"ok": False, "reason": ...}``.
        """
        from agent.batch_runner import get_ordered_seed_list

        queue = self.load_queue()
        if not queue:
            return {"ok": False, "reason": "no_queue"}
        if (queue.get("pending") or []) or (queue.get("failed_retry") or []):
            return {
                "ok": False,
                "reason": "queue_not_complete",
                "pending_count": len(queue.get("pending") or []),
                "failed_retry_count": len(queue.get("failed_retry") or []),
            }
        completed = list(queue.get("completed") or [])
        completed_ids = [e.get("seed_id") for e in completed if isinstance(e, dict) and e.get("seed_id")]

        package = self._load_canonical()
        normal_cont = queue.get("normal_continuation") or copy.deepcopy(DEFAULT_NORMAL_CONTINUATION)
        if package is not None:
            ordered = get_ordered_seed_list(self.market)
            order = {s.get("seed_id"): idx for idx, s in enumerate(ordered or []) if isinstance(s, dict)}
            bs = package.setdefault("batch_state", {})
            current_ids = list(bs.get("processed_seed_ids") or [])
            for cid in completed_ids:
                if cid not in current_ids:
                    current_ids.append(cid)
            current_ids = sorted(current_ids, key=lambda sid: order.get(sid, 10_000_000))
            bs["processed_seed_ids"] = current_ids

            current_seeds = list(bs.get("processed_seeds") or [])
            existing_seed_ids = {
                s.get("seed_id") for s in current_seeds if isinstance(s, dict)
            }
            ordered_lookup = {s.get("seed_id"): s for s in (ordered or []) if isinstance(s, dict)}
            for entry in completed:
                sid = entry.get("seed_id")
                if not sid or sid in existing_seed_ids:
                    continue
                from_ordered = ordered_lookup.get(sid)
                seed_obj = {
                    "seed_id": sid,
                    "make": entry.get("make") or (from_ordered or {}).get("make"),
                    "model": entry.get("model") or (from_ordered or {}).get("model"),
                    "year_start": entry.get("year_start") or (from_ordered or {}).get("year_start"),
                    "year_end": entry.get("year_end") or (from_ordered or {}).get("year_end"),
                    "market": entry.get("market") or (from_ordered or {}).get("market", self.market),
                }
                current_seeds.append(seed_obj)
                existing_seed_ids.add(sid)
            current_seeds = sorted(
                current_seeds,
                key=lambda s: order.get((s or {}).get("seed_id"), 10_000_000),
            )
            bs["processed_seeds"] = current_seeds
            if normal_cont.get("next_seed_id"):
                bs["next_seed_id"] = normal_cont["next_seed_id"]
            if normal_cont.get("last_completed_seed_id"):
                bs["last_completed_seed_id"] = normal_cont["last_completed_seed_id"]
            self._save_canonical(package)

        try:
            self.queue_path.unlink()
            deleted = True
        except FileNotFoundError:
            deleted = False
        return {
            "ok": True,
            "merged_seed_ids": completed_ids,
            "merged_count": len(completed_ids),
            "queue_deleted": deleted,
            "normal_continuation_seed": normal_cont.get("next_seed_id"),
            "mode": "normal_batch",
        }

    # ------------------------------------------------------------------
    # Runner guardrails
    # ------------------------------------------------------------------

    def validate_selected_seed(self, seed_id: str | None) -> dict:
        """Return ``{"ok": True}`` only when the proposed seed is the
        head of the pending queue.  When ``has_pending()`` is false the
        check passes (normal batch may run).
        """
        if not self.queue_exists() or not self.has_pending():
            return {"ok": True, "mode": "normal_batch"}
        head = self.next_seed()
        head_id = head.get("seed_id") if head else None
        if not seed_id:
            return {"ok": False, "reason": "no_seed_selected_while_rerun_pending", "expected_seed_id": head_id}
        normal_next = (self.load_queue().get("normal_continuation") or {}).get("next_seed_id")
        if seed_id == normal_next and head_id != normal_next:
            return {"ok": False, "reason": "normal_continuation_blocked_by_rerun", "expected_seed_id": head_id}
        if seed_id != head_id:
            return {"ok": False, "reason": "selected_seed_is_not_queue_head", "expected_seed_id": head_id}
        return {"ok": True, "mode": "rerun_queue", "selected_seed_id": seed_id}


__all__ = [
    "RerunQueueManager",
    "EXACT_54_RERUN_SEEDS",
    "SCHEMA_VERSION",
    "DEFAULT_QUEUE_PATH",
    "DEFAULT_CANONICAL_PATH",
    "DEFAULT_NORMAL_CONTINUATION",
]
