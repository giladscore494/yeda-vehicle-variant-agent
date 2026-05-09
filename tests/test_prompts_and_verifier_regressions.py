from agent.prompts import build_discovery_prompt, build_verification_prompt, build_retry_discovery_prompt
from agent.verifier import verify_candidates_batch


def test_build_verification_prompt_includes_candidate_value():
    prompt = build_verification_prompt([{"engine": "1.8 Hybrid"}], [])
    assert "1.8 Hybrid" in prompt


def test_build_verification_prompt_includes_source_url():
    prompt = build_verification_prompt([], [{"url": "https://example.com/source"}])
    assert "https://example.com/source" in prompt


def test_build_discovery_prompt_contains_required_candidate_fields():
    class Seed:
        make = "Toyota"
        model = "Corolla"
        year_start = 2017
        year_end = 2023

    prompt = build_discovery_prompt(Seed(), market="IL")
    for field in ["engine", "transmission", "fuel_type", "body_type", "seats", "drivetrain", "generation"]:
        assert field in prompt


def test_build_retry_discovery_prompt_contains_retry_hint():
    """build_retry_discovery_prompt must include the retry instruction and no_variants_reason guidance."""
    class Seed:
        make = "Honda"
        model = "Civic"
        year_start = 2017
        year_end = 2026

    prompt = build_retry_discovery_prompt(Seed(), market="IL")
    assert "RETRY ATTEMPT" in prompt
    assert "no_variants_reason" in prompt
    assert "no_reliable_sources_found" in prompt
    # Base prompt fields still present
    assert "candidate_variants" in prompt
    assert "JSON only" in prompt


def test_build_retry_discovery_prompt_is_superset_of_base():
    """Retry prompt must include everything in the base prompt plus extras."""
    class Seed:
        make = "Hyundai"
        model = "Kona"
        year_start = 2018
        year_end = 2026

    base = build_discovery_prompt(Seed(), market="IL")
    retry = build_retry_discovery_prompt(Seed(), market="IL")
    assert base in retry, "retry prompt must be a superset of the base prompt"
    assert len(retry) > len(base)


def test_verify_candidates_batch_uses_prompt_with_candidate_data(monkeypatch):
    captured = {"prompt": None}

    class FakeGemini:
        def generate_json(self, prompt, strong=True, model_override=None):
            captured["prompt"] = prompt
            return {"ok": True, "data": {"variant_verifications": []}}

    monkeypatch.setattr("agent.verifier.GeminiClient", lambda: FakeGemini())
    verify_candidates_batch([{"engine": "1.8 Hybrid"}], [{"url": "https://example.com/source"}])
    assert "1.8 Hybrid" in captured["prompt"]
    assert "https://example.com/source" in captured["prompt"]


def test_verify_candidates_batch_empty_items_sets_warning(monkeypatch):
    class FakeGemini:
        def generate_json(self, prompt, strong=True, model_override=None):
            return {"ok": True, "data": {"variant_verifications": []}}

    monkeypatch.setattr("agent.verifier.GeminiClient", lambda: FakeGemini())
    out = verify_candidates_batch([{"engine": "1.8 Hybrid"}], [{"url": "https://example.com/source"}])
    assert out["metadata"].get("warning") == "verification_returned_no_items"
