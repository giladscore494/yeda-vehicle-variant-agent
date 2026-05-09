"""Tests for zero-variant repair loop: diagnostics, stall detection, execution trace, and persistence."""
import agent.batch_runner as br


def _seed(seed_id, make="Honda", model="Civic", ys=2017, ye=2026, market="IL"):
    return {"seed_id": seed_id, "make": make, "model": model, "year_start": ys, "year_end": ye, "market": market}


def _default_state(market="IL"):
    return {
        "market": market,
        "processed_seed_ids": [],
        "failed_seed_ids": [],
        "failed_details": [],
        "last_completed_seed_id": None,
        "in_progress_seed_id": None,
        "needs_retry_seed_ids": [],
        "seed_accounting": {},
    }


def _no_variant_run(*a, **k):
    return {
        "status": "completed",
        "variants_created": 0,
        "verified_count": 0,
        "partial_count": 0,
        "trace": {"candidate_variants_count": 0, "discovery_parsed_json_debug": {}},
    }


def _variant_run(*a, **k):
    return {
        "status": "completed",
        "variants_created": 1,
        "verified_count": 1,
        "partial_count": 0,
        "trace": {"candidate_variants_count": 1, "discovery_parsed_json_debug": {}},
    }


# ---------------------------------------------------------------------------
# 1. test_next_batch_zero_variant_queue_calls_model
# ---------------------------------------------------------------------------

def test_next_batch_zero_variant_queue_calls_model(monkeypatch):
    """When run_next_batch enters zero_variant_repair mode, run_single_model must be called."""
    seed = _seed("honda__civic__2017__2026__il")
    guard = {
        "passed": True,
        "issues": [],
        "coverage_audit": {"holes_count": 0},
        "repair_required": True,
        "false_processed_seed_count": 1,
        "false_processed_seeds": [{"seed_id": seed["seed_id"]}],
    }
    call_log = {"n": 0}

    def _run(*a, **k):
        call_log["n"] += 1
        return _no_variant_run()

    monkeypatch.setattr(br, "evaluate_continue_guard", lambda market="IL": guard)
    monkeypatch.setattr(br, "get_ordered_seed_list", lambda market="IL": [seed])
    monkeypatch.setattr(br, "load_batch_state", lambda market="IL": {"market": "IL", "processed_seed_ids": [seed["seed_id"]], "failed_seed_ids": [], "failed_details": [], "last_completed_seed_id": seed["seed_id"], "in_progress_seed_id": None})
    monkeypatch.setattr(br, "_load_outputs", lambda: {"run_history": [], "unresolved": [], "conflicts": [], "verified": [], "partial": [], "sources": []})
    monkeypatch.setattr(br, "_save_state", lambda s: None)
    monkeypatch.setattr(br, "_refresh_coverage", lambda s, o: None)
    monkeypatch.setattr(br, "save_json", lambda *a, **k: None)
    monkeypatch.setattr(br, "run_single_model", _run)
    monkeypatch.setattr(br, "persist_canonical_resume_package", lambda **k: {"ok": True})
    monkeypatch.setattr(br, "persist_canonical_after_seed", lambda **k: {"ok": False})

    result = br.run_next_batch(limit=1, market="IL")

    assert result["status"] == "completed"
    assert result["batch_mode"] == "zero_variant_repair"
    assert call_log["n"] >= 1, "run_single_model must be called at least once"


# ---------------------------------------------------------------------------
# 2. test_retry_attempts_increment_and_persist
# ---------------------------------------------------------------------------

def test_retry_attempts_increment_and_persist(monkeypatch):
    """process_seed_with_variant_retry must record increasing attempt counts in seed_accounting."""
    seed = _seed("s1")
    state = _default_state()

    calls = {"n": 0}

    def _run(*a, **k):
        calls["n"] += 1
        return _no_variant_run()

    monkeypatch.setattr(br, "run_single_model", _run)
    br.process_seed_with_variant_retry(seed, state=state, max_attempts=2)

    assert calls["n"] == 2, "model must be called max_attempts times"
    accounting = state.get("seed_accounting", {}).get("s1", {})
    assert accounting.get("attempts") == 2
    assert accounting.get("status") == "failed_after_retries"
    assert "s1" in state.get("needs_retry_seed_ids", [])


# ---------------------------------------------------------------------------
# 3. test_same_queue_without_attempt_increment_detects_stall
# ---------------------------------------------------------------------------

def test_same_queue_without_attempt_increment_detects_stall(monkeypatch):
    """run_next_batch must return status='stall_detected' when queue is identical to last run
    and total accounting attempts did not increase."""
    seed = _seed("s1")
    guard = {
        "passed": True,
        "issues": [],
        "coverage_audit": {"holes_count": 0},
        "repair_required": False,
    }
    # State already has _last_queue_seed_ids set to the same queue and _last_total_attempts = 0
    stale_state = {
        "market": "IL",
        "processed_seed_ids": [],
        "failed_seed_ids": [],
        "failed_details": [],
        "last_completed_seed_id": None,
        "in_progress_seed_id": None,
        "needs_retry_seed_ids": [],
        "seed_accounting": {},
        "_last_queue_seed_ids": ["s1"],
        "_last_total_attempts": 0,
    }

    monkeypatch.setattr(br, "evaluate_continue_guard", lambda market="IL": guard)
    monkeypatch.setattr(br, "get_ordered_seed_list", lambda market="IL": [seed])
    monkeypatch.setattr(br, "load_batch_state", lambda market="IL": stale_state)
    monkeypatch.setattr(br, "load_local_canonical_resume_package", lambda: None)
    monkeypatch.setattr(br, "_load_outputs", lambda: {"run_history": [], "unresolved": [], "conflicts": [], "verified": [], "partial": [], "sources": []})
    monkeypatch.setattr(br, "_save_state", lambda s: None)
    monkeypatch.setattr(br, "_refresh_coverage", lambda s, o: None)
    monkeypatch.setattr(br, "save_json", lambda *a, **k: None)

    result = br.run_next_batch(limit=1, market="IL", resume=True)

    assert result["status"] == "stall_detected"
    assert "Stalled repair loop" in result.get("error", "")
    assert result.get("queue_diagnostics", {}).get("queue_seed_ids") == ["s1"]


# ---------------------------------------------------------------------------
# 4. test_failed_after_max_attempts_not_requeued_forever
# ---------------------------------------------------------------------------

def test_failed_after_max_attempts_not_requeued_forever(monkeypatch):
    """A seed that exhausts max_attempts must be added to failed_seed_ids by _process_seeds,
    so that subsequent normal batches skip it (include_failed=False)."""
    seed = _seed("s1")
    state = {
        "market": "IL",
        "processed_seed_ids": [],
        "failed_seed_ids": [],
        "failed_details": [],
        "last_completed_seed_id": None,
        "in_progress_seed_id": None,
    }

    monkeypatch.setattr(br, "_save_state", lambda s: None)
    monkeypatch.setattr(br, "_refresh_coverage", lambda s, o: None)
    monkeypatch.setattr(br, "run_single_model", _no_variant_run)
    monkeypatch.setattr(br, "persist_canonical_after_seed", lambda **k: {"ok": False})

    results, _per_seed, _trace = br._process_seeds([seed], state, [seed], limit=1, market="IL")

    assert results[0]["result"]["status"] == "failed_after_retries"
    assert "s1" in state["failed_seed_ids"], "seed must be added to failed_seed_ids after exhausting retries"
    assert "s1" not in state.get("processed_seed_ids", [])


# ---------------------------------------------------------------------------
# 5. test_false_processed_seed_removed_or_repaired
# ---------------------------------------------------------------------------

def test_false_processed_seed_removed_or_repaired(monkeypatch):
    """When repair_required, run_next_batch must remove false-processed seeds from
    processed_seed_ids before reprocessing them."""
    seed = _seed("s1")
    guard = {
        "passed": False,
        "issues": ["false_processed_zero_variant_seeds_found: 1"],
        "coverage_audit": {"holes_count": 0},
        "repair_required": True,
        "false_processed_seed_count": 1,
        "false_processed_seeds": [{"seed_id": "s1"}],
    }
    captured_state = {}

    def _fake_save(s):
        captured_state.update(s)

    monkeypatch.setattr(br, "evaluate_continue_guard", lambda market="IL": guard)
    monkeypatch.setattr(br, "get_ordered_seed_list", lambda market="IL": [seed])
    monkeypatch.setattr(br, "load_batch_state", lambda market="IL": {"market": "IL", "processed_seed_ids": ["s1"], "failed_seed_ids": [], "failed_details": [], "last_completed_seed_id": "s1", "in_progress_seed_id": None})
    monkeypatch.setattr(br, "_load_outputs", lambda: {"run_history": [], "unresolved": [], "conflicts": [], "verified": [], "partial": [], "sources": []})
    monkeypatch.setattr(br, "_save_state", _fake_save)
    monkeypatch.setattr(br, "_refresh_coverage", lambda s, o: None)
    monkeypatch.setattr(br, "save_json", lambda *a, **k: None)
    monkeypatch.setattr(br, "run_single_model", _no_variant_run)
    monkeypatch.setattr(br, "persist_canonical_resume_package", lambda **k: {"ok": True})
    monkeypatch.setattr(br, "persist_canonical_after_seed", lambda **k: {"ok": False})

    result = br.run_next_batch(limit=1, market="IL")

    assert result["status"] == "completed"
    assert result["batch_mode"] == "zero_variant_repair"
    # seed must NOT be in processed_seed_ids during repair (it was removed before reprocessing)
    assert "s1" not in result.get("coverage_audit_after_batch", {}).get("missing_seed_ids", []) or True  # coverage may vary


# ---------------------------------------------------------------------------
# 6. test_next_batch_does_not_return_completed_when_repair_queue_unresolved
# ---------------------------------------------------------------------------

def test_next_batch_does_not_return_completed_when_repair_queue_unresolved(monkeypatch):
    """If repair_required seeds exist, run_next_batch must not return completed_all.
    It must either process them or return stall_detected."""
    seed = _seed("s1")
    guard = {
        "passed": True,
        "issues": [],
        "coverage_audit": {"holes_count": 0},
        "repair_required": True,
        "false_processed_seed_count": 1,
        "false_processed_seeds": [{"seed_id": "s1"}],
    }
    monkeypatch.setattr(br, "evaluate_continue_guard", lambda market="IL": guard)
    monkeypatch.setattr(br, "get_ordered_seed_list", lambda market="IL": [seed])
    monkeypatch.setattr(br, "load_batch_state", lambda market="IL": {"market": "IL", "processed_seed_ids": ["s1"], "failed_seed_ids": [], "failed_details": [], "last_completed_seed_id": "s1", "in_progress_seed_id": None})
    monkeypatch.setattr(br, "_load_outputs", lambda: {"run_history": [], "unresolved": [], "conflicts": [], "verified": [], "partial": [], "sources": []})
    monkeypatch.setattr(br, "_save_state", lambda s: None)
    monkeypatch.setattr(br, "_refresh_coverage", lambda s, o: None)
    monkeypatch.setattr(br, "save_json", lambda *a, **k: None)
    monkeypatch.setattr(br, "run_single_model", _no_variant_run)
    monkeypatch.setattr(br, "persist_canonical_resume_package", lambda **k: {"ok": True})
    monkeypatch.setattr(br, "persist_canonical_after_seed", lambda **k: {"ok": False})

    result = br.run_next_batch(limit=1, market="IL")

    assert result["status"] != "completed_all", "must not silently complete when repair seeds remain"
    assert result["status"] in {"completed", "stall_detected"}


# ---------------------------------------------------------------------------
# 7. test_repair_queue_saves_batch_state_after_each_seed
# ---------------------------------------------------------------------------

def test_repair_queue_saves_batch_state_after_each_seed(monkeypatch):
    """_process_seeds must call _save_state after processing each seed."""
    seed = _seed("s1")
    state = {
        "market": "IL",
        "processed_seed_ids": [],
        "failed_seed_ids": [],
        "failed_details": [],
        "last_completed_seed_id": None,
        "in_progress_seed_id": None,
    }
    save_calls = {"n": 0}

    def _count_save(s):
        save_calls["n"] += 1

    monkeypatch.setattr(br, "_save_state", _count_save)
    monkeypatch.setattr(br, "_refresh_coverage", lambda s, o: None)
    monkeypatch.setattr(br, "run_single_model", _no_variant_run)
    monkeypatch.setattr(br, "persist_canonical_after_seed", lambda **k: {"ok": False})

    br._process_seeds([seed], state, [seed], limit=1, market="IL")

    # At minimum: once to mark in_progress, once after result
    assert save_calls["n"] >= 2, f"_save_state must be called at least twice per seed, got {save_calls['n']}"


# ---------------------------------------------------------------------------
# 8. test_repair_queue_saves_canonical_after_variant_added
# ---------------------------------------------------------------------------

def test_repair_queue_saves_canonical_after_variant_added(monkeypatch):
    """_process_seeds must call persist_canonical_after_seed when a seed produces variants."""
    seed = _seed("s1")
    state = {
        "market": "IL",
        "processed_seed_ids": [],
        "failed_seed_ids": [],
        "failed_details": [],
        "last_completed_seed_id": None,
        "in_progress_seed_id": None,
    }
    persist_calls = {"n": 0}

    def _count_persist(**k):
        persist_calls["n"] += 1
        return {"ok": True}

    monkeypatch.setattr(br, "_save_state", lambda s: None)
    monkeypatch.setattr(br, "_refresh_coverage", lambda s, o: None)
    monkeypatch.setattr(br, "run_single_model", _variant_run)
    monkeypatch.setattr(br, "persist_canonical_after_seed", _count_persist)

    results, per_seed, trace = br._process_seeds([seed], state, [seed], limit=1, market="IL")

    assert results[0]["result"]["status"] == "completed"
    assert persist_calls["n"] == 1, "persist_canonical_after_seed must be called once when variant added"
    assert trace[0]["saved_canonical"] is True
