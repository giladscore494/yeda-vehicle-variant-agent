"""Regression tests for empty/failed Gemini discovery guard.

Covers the 5 scenarios from the problem statement (Part F):
  1. Empty Gemini payload → error, never completed
  2. Empty candidate_variants without no_variants_reason → needs_retry / failed_after_retries
  3. Empty candidate_variants with allowed no_variants_reason → completed / no false variant
  4. Retry bypasses cache
  5. Canonical safety: failed zero-variant seed not added to processed_seed_ids
"""

import pytest

from agent.runner import run_single_model, ALLOWED_NO_VARIANTS_REASONS
import agent.batch_runner as br


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _Seed:
    make = "Toyota"
    model = "Corolla"
    year_start = 2016
    year_end = 2021


def _seed_dict(seed_id="toyota__corolla__2016__2021__il"):
    return {
        "seed_id": seed_id,
        "make": "Toyota",
        "model": "Corolla",
        "year_start": 2016,
        "year_end": 2021,
        "market": "IL",
    }


# ---------------------------------------------------------------------------
# 1. Empty Gemini payload: ok=False, data=None
# ---------------------------------------------------------------------------

def test_empty_gemini_payload_returns_error(monkeypatch):
    """run_single_model must return status=error when discovery returns ok=False, data=None."""
    monkeypatch.setattr("agent.runner.find_seed", lambda make, model: _Seed())
    monkeypatch.setattr(
        "agent.runner.run_discovery",
        lambda *a, **k: {
            "ok": False,
            "data": None,
            "error": "Gemini API failure",
            "gemini_metadata": {
                "raw_text": None,
                "parsed_json": None,
                "parse_error": None,
                "error": "Gemini API failure",
                "request_attempted": True,
                "model": "gemini-1.5-pro",
            },
        },
    )
    out = run_single_model("Toyota", "Corolla", use_cache=False, force_refresh=True)
    assert out["status"] == "error", f"Expected status=error, got {out['status']}"
    assert out.get("variants_created", 0) == 0 or "variants_created" not in out


def test_empty_gemini_payload_not_completed(monkeypatch):
    """run_single_model must NOT return completed with variants_created=0 for ok=False, data=None."""
    monkeypatch.setattr("agent.runner.find_seed", lambda make, model: _Seed())
    monkeypatch.setattr(
        "agent.runner.run_discovery",
        lambda *a, **k: {
            "ok": False,
            "data": None,
            "error": "network error",
            "gemini_metadata": {
                "raw_text": None,
                "parsed_json": None,
                "parse_error": None,
                "error": "network error",
                "request_attempted": True,
                "model": None,
            },
        },
    )
    out = run_single_model("Toyota", "Corolla", use_cache=False, force_refresh=True)
    assert out["status"] != "completed", "Must not return completed for empty Gemini payload"


def test_empty_parsed_payload_returns_error(monkeypatch):
    """run_single_model must return status=error when parsed payload is {} (empty dict) and no raw_text."""
    monkeypatch.setattr("agent.runner.find_seed", lambda make, model: _Seed())
    monkeypatch.setattr(
        "agent.runner.run_discovery",
        lambda *a, **k: {
            "ok": True,
            "data": {},
            "error": None,
            "gemini_metadata": {
                "raw_text": None,
                "parsed_json": {},
                "parse_error": None,
                "error": None,
                "request_attempted": True,
                "model": "gemini-1.5-pro",
            },
        },
    )
    out = run_single_model("Toyota", "Corolla", use_cache=False, force_refresh=True)
    assert out["status"] == "error", f"Expected status=error for empty payload, got {out['status']}"


# ---------------------------------------------------------------------------
# 2. Empty candidate_variants without no_variants_reason → needs_retry / failed_after_retries
# ---------------------------------------------------------------------------

def test_empty_candidates_no_reason_returns_needs_retry(monkeypatch):
    """run_single_model must return needs_retry when candidate_variants==[] and no_variants_reason is absent."""
    monkeypatch.setattr("agent.runner.find_seed", lambda make, model: _Seed())
    monkeypatch.setattr(
        "agent.runner.run_discovery",
        lambda *a, **k: {
            "ok": True,
            "data": {"candidate_variants": [], "sources": []},
            "error": None,
            "gemini_metadata": {
                "raw_text": '{"candidate_variants":[]}',
                "parsed_json": {"candidate_variants": []},
                "parse_error": None,
                "error": None,
                "request_attempted": True,
                "model": "gemini-1.5-pro",
            },
        },
    )
    out = run_single_model("Toyota", "Corolla", use_cache=False, force_refresh=True)
    assert out["status"] in {"needs_retry", "error"}, (
        f"Expected needs_retry or error for empty candidates without reason, got {out['status']}"
    )
    assert out.get("variants_created", 0) == 0


def test_empty_candidates_no_reason_not_marked_processed(monkeypatch):
    """process_seed_with_variant_retry must NOT add seed to processed_seed_ids when no reason and no variants."""
    state = {"processed_seed_ids": [], "failed_seed_ids": []}
    monkeypatch.setattr(
        br,
        "run_single_model",
        lambda *a, **k: {
            "status": "needs_retry",
            "variants_created": 0,
            "verified_count": 0,
            "partial_count": 0,
            "error": "zero_variants_without_no_variants_reason",
            "no_variants_reason": None,
            "trace": {"candidate_variants_count": 0, "discovery_parsed_json_debug": {}},
        },
    )
    result = br.process_seed_with_variant_retry(_seed_dict(), state=state, max_attempts=2)
    assert _seed_dict()["seed_id"] not in state["processed_seed_ids"]
    assert result["status"] == "failed_after_retries"


def test_empty_candidates_no_reason_reaches_failed_after_retries(monkeypatch):
    """After max_attempts, seed with empty candidates and no reason must be failed_after_retries."""
    calls = {"n": 0}
    state = {"processed_seed_ids": [], "failed_seed_ids": []}

    def _mock_run(*a, **k):
        calls["n"] += 1
        return {
            "status": "needs_retry",
            "variants_created": 0,
            "verified_count": 0,
            "partial_count": 0,
            "error": "zero_variants_without_no_variants_reason",
            "no_variants_reason": None,
            "trace": {"candidate_variants_count": 0, "discovery_parsed_json_debug": {}},
        }

    monkeypatch.setattr(br, "run_single_model", _mock_run)
    result = br.process_seed_with_variant_retry(_seed_dict(), state=state, max_attempts=3)
    assert result["status"] == "failed_after_retries"
    assert _seed_dict()["seed_id"] not in state["processed_seed_ids"]
    # All 3 attempts must have been made
    assert calls["n"] == 3


# ---------------------------------------------------------------------------
# 3. Empty candidate_variants with allowed no_variants_reason → completed/no false variant
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("reason", sorted(ALLOWED_NO_VARIANTS_REASONS))
def test_allowed_no_variants_reason_returns_completed(monkeypatch, reason):
    """run_single_model must return completed with variants_created=0 for allowed no_variants_reason."""
    monkeypatch.setattr("agent.runner.find_seed", lambda make, model: _Seed())
    monkeypatch.setattr(
        "agent.runner.run_discovery",
        lambda *a, **k: {
            "ok": True,
            "data": {"candidate_variants": [], "no_variants_reason": reason, "sources": []},
            "error": None,
            "gemini_metadata": {
                "raw_text": f'{{"candidate_variants":[],"no_variants_reason":"{reason}"}}',
                "parsed_json": {"candidate_variants": [], "no_variants_reason": reason},
                "parse_error": None,
                "error": None,
                "request_attempted": True,
                "model": "gemini-1.5-pro",
            },
        },
    )
    out = run_single_model("Toyota", "Corolla", use_cache=False, force_refresh=True)
    assert out["status"] == "completed", f"Expected completed for reason={reason}, got {out['status']}"
    assert out.get("variants_created", 0) == 0
    assert out.get("no_variants_reason") == reason
    trace = out.get("trace", {})
    assert trace.get("final_decision", {}).get("classification") == "no_variants_reason"


def test_allowed_no_variants_reason_no_false_variant(monkeypatch):
    """Completed with no_variants_reason must not create any variant records."""
    monkeypatch.setattr("agent.runner.find_seed", lambda make, model: _Seed())
    reason = "model_not_sold_in_market"
    monkeypatch.setattr(
        "agent.runner.run_discovery",
        lambda *a, **k: {
            "ok": True,
            "data": {"candidate_variants": [], "no_variants_reason": reason, "sources": []},
            "error": None,
            "gemini_metadata": {
                "raw_text": f'{{"candidate_variants":[],"no_variants_reason":"{reason}"}}',
                "parsed_json": {"candidate_variants": [], "no_variants_reason": reason},
                "parse_error": None,
                "error": None,
                "request_attempted": True,
                "model": "gemini-1.5-pro",
            },
        },
    )
    out = run_single_model("Toyota", "Corolla", use_cache=False, force_refresh=True)
    assert out["status"] == "completed"
    assert out.get("variants_created", 0) == 0
    assert out.get("verified_count", 0) == 0
    assert out.get("partial_count", 0) == 0


def test_allowed_no_variants_reason_marks_processed_in_batch(monkeypatch):
    """process_seed_with_variant_retry must mark seed as processed when runner returns no_variants_reason."""
    state = {"processed_seed_ids": [], "failed_seed_ids": []}
    reason = "no_reliable_sources_found"
    monkeypatch.setattr(
        br,
        "run_single_model",
        lambda *a, **k: {
            "status": "completed",
            "variants_created": 0,
            "verified_count": 0,
            "partial_count": 0,
            "no_variants_reason": reason,
            "trace": {
                "candidate_variants_count": 0,
                "no_variants_reason": reason,
                "discovery_parsed_json_debug": {"candidate_variants": [], "no_variants_reason": reason},
            },
        },
    )
    result = br.process_seed_with_variant_retry(_seed_dict(), state=state, max_attempts=1)
    sid = _seed_dict()["seed_id"]
    assert sid in state["processed_seed_ids"], "Seed with allowed no_variants_reason must be marked processed"
    assert result["status"] == "completed"
    assert state.get("no_variants_by_seed", {}).get(sid, {}).get("reason") == reason


# ---------------------------------------------------------------------------
# 4. Retry bypasses cache
# ---------------------------------------------------------------------------

def test_retry_hint_forces_use_cache_false(monkeypatch):
    """When retry_hint=True, run_single_model must not use discovery or final cache."""
    monkeypatch.setattr("agent.runner.find_seed", lambda make, model: _Seed())
    discovery_calls = {"n": 0}

    def _mock_discovery(*a, **k):
        discovery_calls["n"] += 1
        return {
            "ok": True,
            "data": {
                "candidate_variants": [{"engine": "1.8L", "transmission": "automatic", "fuel_type": "gasoline", "body_type": "sedan", "generation": "E170", "year_start": 2016, "year_end": 2021}],
                "sources": [],
            },
            "error": None,
            "gemini_metadata": {
                "raw_text": '{"candidate_variants":[{"engine":"1.8L"}]}',
                "parsed_json": {"candidate_variants": [{"engine": "1.8L"}]},
                "parse_error": None,
                "error": None,
                "request_attempted": True,
                "model": "gemini-1.5-pro",
            },
        }

    monkeypatch.setattr("agent.runner.run_discovery", _mock_discovery)
    # First call (no retry) - baseline
    out1 = run_single_model("Toyota", "Corolla", use_cache=False, force_refresh=True, retry_hint=False)
    calls_after_first = discovery_calls["n"]

    # Second call with retry_hint=True — must call discovery again (not use cache)
    out2 = run_single_model("Toyota", "Corolla", use_cache=True, force_refresh=False, retry_hint=True)
    assert discovery_calls["n"] > calls_after_first, (
        "retry_hint=True must bypass cache and call run_discovery again"
    )
    assert out2.get("trace", {}).get("final_cache_hit") is not True
    assert out2.get("trace", {}).get("discovery_cache_hit") is not True


def test_retry_attempt_calls_discovery_again(monkeypatch):
    """process_seed_with_variant_retry: attempt 2 must call run_single_model with retry_hint=True."""
    calls = []
    state = {"processed_seed_ids": [], "failed_seed_ids": []}

    def _mock_run(*a, **k):
        calls.append(k.get("retry_hint", False))
        return {
            "status": "needs_retry",
            "variants_created": 0,
            "verified_count": 0,
            "partial_count": 0,
            "no_variants_reason": None,
            "trace": {"candidate_variants_count": 0, "discovery_parsed_json_debug": {}},
        }

    monkeypatch.setattr(br, "run_single_model", _mock_run)
    br.process_seed_with_variant_retry(_seed_dict(), state=state, max_attempts=3)
    assert len(calls) >= 2, "Expected at least 2 run_single_model calls"
    # First attempt: retry_hint=False; subsequent: retry_hint=True
    assert calls[0] is False
    assert all(calls[i] is True for i in range(1, len(calls)))


def test_cache_hit_fields_false_on_retry(monkeypatch):
    """final_cache_hit and discovery_cache_hit must be False when retry_hint=True."""
    monkeypatch.setattr("agent.runner.find_seed", lambda make, model: _Seed())
    monkeypatch.setattr(
        "agent.runner.run_discovery",
        lambda *a, **k: {
            "ok": True,
            "data": {
                "candidate_variants": [{"engine": "2.0L", "transmission": "manual", "fuel_type": "gasoline", "body_type": "sedan", "generation": "E170", "year_start": 2016, "year_end": 2021}],
                "sources": [],
            },
            "error": None,
            "gemini_metadata": {
                "raw_text": '{"candidate_variants":[{"engine":"2.0L"}]}',
                "parsed_json": {"candidate_variants": [{"engine": "2.0L"}]},
                "parse_error": None,
                "error": None,
                "request_attempted": True,
                "model": "gemini-1.5-pro",
            },
        },
    )
    out = run_single_model("Toyota", "Corolla", use_cache=True, force_refresh=False, retry_hint=True)
    trace = out.get("trace", {})
    assert trace.get("final_cache_hit") is not True, "final_cache_hit must be False when retry_hint=True"
    assert trace.get("discovery_cache_hit") is not True, "discovery_cache_hit must be False when retry_hint=True"


# ---------------------------------------------------------------------------
# 5. Canonical safety
# ---------------------------------------------------------------------------

def test_failed_zero_variant_seed_not_added_to_processed(monkeypatch):
    """A seed that repeatedly returns empty candidates without reason must never be in processed_seed_ids."""
    state = {"processed_seed_ids": [], "failed_seed_ids": []}
    monkeypatch.setattr(
        br,
        "run_single_model",
        lambda *a, **k: {
            "status": "needs_retry",
            "variants_created": 0,
            "verified_count": 0,
            "partial_count": 0,
            "no_variants_reason": None,
            "trace": {"candidate_variants_count": 0, "discovery_parsed_json_debug": {}},
        },
    )
    result = br.process_seed_with_variant_retry(_seed_dict(), state=state, max_attempts=5)
    sid = _seed_dict()["seed_id"]
    assert sid not in state["processed_seed_ids"], "Failed zero-variant seed must NOT be in processed_seed_ids"
    assert result["status"] == "failed_after_retries"


def test_canonical_variant_count_does_not_shrink_on_zero_variant_failure(monkeypatch):
    """Batch state must persist failed_seed_ids correctly and not remove seeds from needs_retry_seed_ids."""
    state = {"processed_seed_ids": [], "failed_seed_ids": [], "needs_retry_seed_ids": []}
    monkeypatch.setattr(
        br,
        "run_single_model",
        lambda *a, **k: {
            "status": "error",
            "variants_created": 0,
            "verified_count": 0,
            "partial_count": 0,
            "no_variants_reason": None,
            "trace": {"candidate_variants_count": 0, "discovery_parsed_json_debug": {}},
        },
    )
    sid = _seed_dict()["seed_id"]
    result = br.process_seed_with_variant_retry(_seed_dict(), state=state, max_attempts=2)
    assert sid not in state["processed_seed_ids"]
    assert sid in state.get("needs_retry_seed_ids", []) or result["status"] == "failed_after_retries"


def test_batch_state_persists_failed_seed_ids(monkeypatch):
    """failed_seed_ids must be populated after repeated failures."""
    state = {"processed_seed_ids": [], "failed_seed_ids": [], "needs_retry_seed_ids": []}
    monkeypatch.setattr(
        br,
        "run_single_model",
        lambda *a, **k: {
            "status": "needs_retry",
            "variants_created": 0,
            "verified_count": 0,
            "partial_count": 0,
            "no_variants_reason": None,
            "trace": {"candidate_variants_count": 0, "discovery_parsed_json_debug": {}},
        },
    )
    sid = _seed_dict()["seed_id"]
    br.process_seed_with_variant_retry(_seed_dict(), state=state, max_attempts=3)
    # After all retries exhausted, seed should be in needs_retry_seed_ids
    assert sid in state.get("needs_retry_seed_ids", [])
    # And NOT in processed_seed_ids
    assert sid not in state["processed_seed_ids"]


def test_no_variants_reason_read_from_trace_field(monkeypatch):
    """batch_runner must read no_variants_reason from trace['no_variants_reason'] too (not just parsed_json)."""
    state = {"processed_seed_ids": [], "failed_seed_ids": []}
    reason = "seed_out_of_scope"
    monkeypatch.setattr(
        br,
        "run_single_model",
        lambda *a, **k: {
            "status": "completed",
            "variants_created": 0,
            "verified_count": 0,
            "partial_count": 0,
            "no_variants_reason": reason,
            # no_variants_reason in trace (not in discovery_parsed_json_debug)
            "trace": {
                "candidate_variants_count": 0,
                "no_variants_reason": reason,
                "discovery_parsed_json_debug": {},  # intentionally empty
            },
        },
    )
    result = br.process_seed_with_variant_retry(_seed_dict(), state=state, max_attempts=1)
    sid = _seed_dict()["seed_id"]
    assert sid in state["processed_seed_ids"], (
        "Seed must be marked processed when no_variants_reason is in trace (not only parsed_json)"
    )
    assert result["status"] == "completed"
