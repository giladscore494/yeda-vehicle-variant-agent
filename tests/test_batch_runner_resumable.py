from agent.batch_runner import build_seed_id, build_final_export, get_ordered_seed_list


def test_seed_id_stable():
    assert build_seed_id("Abarth", "500", 2008, 2026, "IL") == "abarth__500__2008__2026__il"


def test_ordered_seed_list_deterministic():
    ordered = get_ordered_seed_list("IL")
    keys = [(s["make"].lower(), s["model"].lower(), s["year_start"], s["year_end"]) for s in ordered]
    assert keys == sorted(keys)


def test_build_final_export_shape():
    payload = build_final_export(include_partial=True, include_verified=True)
    assert payload["schema_version"] == "vehicle_variants_final_v1"
    assert "variants" in payload and isinstance(payload["variants"], list)
