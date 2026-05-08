from agent.prompts import build_discovery_prompt, build_verification_prompt
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
