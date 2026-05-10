from __future__ import annotations
import copy
import agent.batch_runner as br
import app as app_mod


def _seed(seed_id):
    return {"seed_id": seed_id, "make": "M", "model": "X", "year_start": 2018, "year_end": 2026, "market": "IL"}


def test_recover_active_state_restores_backup_retry_and_preserves_variants(monkeypatch):
    seeds = [_seed("bmw__850i__2018__2026__il"), _seed("haval__h6__2022__2026__il"), _seed("gmc__yukon__2000__2026__il")]
    retry = ["bmw__850i__2018__2026__il"] + [f"make__{i}__model__2018__2026__il" for i in range(53)]
    ordered = seeds + [_seed(x) for x in retry[1:]]
    current = {"batch_state": {"processed_seed_ids": [s["seed_id"] for s in ordered], "needs_retry_seed_ids": ["s1"], "next_seed_id": "haval__h6__2022__2026__il", "last_completed_seed_id": "gmc__yukon__2000__2026__il"}, "accumulated_clean_export": {"variants": [{"variant_id": str(i)} for i in range(1323)]}}
    backup = {"batch_state": {"needs_retry_seed_ids": retry}}
    saved = {}
    monkeypatch.setattr(br, "get_ordered_seed_list", lambda market="IL": ordered)
    monkeypatch.setattr(br, "load_local_canonical_resume_package", lambda: copy.deepcopy(current))
    monkeypatch.setattr(br, "_canonical_backup_path", lambda: type("P", (), {"exists": lambda self: True})())
    monkeypatch.setattr(br, "load_json_object", lambda p: copy.deepcopy(backup))
    monkeypatch.setattr(br, "save_local_canonical_resume_package", lambda pkg: saved.setdefault("pkg", copy.deepcopy(pkg)))
    monkeypatch.setattr(br, "_save_state", lambda st: saved.setdefault("state", copy.deepcopy(st)))

    out = br.recover_active_state_from_current_canonical_and_backup("IL")
    assert out["ok"] is True
    assert out["recovered_needs_retry_count"] == 54
    assert out["current_repair_seed"] == "bmw__850i__2018__2026__il"
    assert "s1" in out["invalid_needs_retry_seed_ids"]
    assert out["variants_count_after"] == 1323
    assert out["next_normal_seed"] == "haval__h6__2022__2026__il"


def test_run_next_batch_never_completed_all_when_needs_retry_present(monkeypatch):
    ordered = [_seed("abarth__124_spider__2016__2020__il"), _seed("bmw__850i__2018__2026__il")]
    state = {"processed_seed_ids": [], "needs_retry_seed_ids": ["bmw__850i__2018__2026__il"], "next_seed_id": "abarth__124_spider__2016__2020__il", "last_completed_seed_id": None, "failed_seed_ids": []}
    monkeypatch.setattr(br, "evaluate_continue_guard", lambda market="IL": {"passed": True, "repair_required": False, "needs_retry_required": True, "false_processed_seeds": []})
    monkeypatch.setattr(br, "get_ordered_seed_list", lambda market="IL": ordered)
    monkeypatch.setattr(br, "sync_batch_state_from_canonical", lambda market="IL": copy.deepcopy(state))
    monkeypatch.setattr(br, "_ensure_zero_variant_fields", lambda s: s)
    monkeypatch.setattr(br, "sanitize_repair_queue_state", lambda s,o: s)
    monkeypatch.setattr(br, "_load_outputs", lambda: {})
    monkeypatch.setattr(br, "audit_coverage_until_last_completed", lambda *a, **k: {"missing_seeds": []})
    monkeypatch.setattr(br, "run_single_model", lambda **k: {"status": "needs_retry"})
    res = br.run_next_batch(limit=1, market="IL", resume=True)
    assert res.get("status") != "completed_all"


def test_status_snapshot_uses_canonical_not_abarth(monkeypatch):
    # The new _status_snapshot reads from load_problem_queue_canonical (canonical-first).
    # Patch it to return a canonical with the expected next_seed_id.
    canonical = {
        "batch_state": {
            "processed_seed_ids": ["x"],
            "needs_retry_seed_ids": [],
            "next_seed_id": "haval__h6__2022__2026__il",
        },
        "problem_repair_state": {
            "active": False,
            "total": 0,
            "progress": {"completed": 0, "pending": 0, "failed_retry": 0, "current_position": "0 / 0"},
            "normal_continuation": {"next_seed_id": "haval__h6__2022__2026__il"},
            "current_seed_id": None,
            "last_completed_seed_id": None,
        },
        "accumulated_clean_export": {"variants": []},
    }
    monkeypatch.setattr(app_mod, "load_problem_queue_canonical", lambda: canonical)
    snap = app_mod._status_snapshot("IL")
    assert snap["next_normal_seed"] == "haval__h6__2022__2026__il"
