import json

from core.schemas import VehicleModelSeed


def build_discovery_prompt(seed: VehicleModelSeed, market="IL") -> str:
    return f"""Return compact JSON only. JSON only. No prose. No markdown.
Reason max 120 chars for unresolved_reason only.
Research make={seed.make}, model={seed.model}, year_start={seed.year_start}, year_end={seed.year_end}, market={market}.
Return valid minified JSON only.
No markdown.
No prose.
No trailing commas.
No unfinished fields.
Do not include notes.
Do not include explanations.
Do not include reason strings.
Return max 8 candidate_variants and max 5 sources.
Do not include evidence_snippets by default.
If evidence_snippets are included, max 1 per source and max 80 chars.
Top-level keys: search_queries, sources, candidate_variants, conflicts, unresolved, unresolved_reason.
Candidate shape:
{{
  "candidate_index": 0,
  "make": "",
  "model": "",
  "year_start": 2009,
  "year_end": 2014,
  "generation": "",
  "body_type": "",
  "seats": 5,
  "engine": "",
  "transmission": "",
  "fuel_type": "",
  "drivetrain": "",
  "trim": "",
  "source_ids": [],
  "field_sources": {{
    "body_type": [], "seats": [], "engine": [], "transmission": [], "fuel_type": [], "drivetrain": [], "generation": [], "year_start": [], "year_end": [], "trim": []
  }}
}}
Source shape:
{{
  "source_id": "src_1",
  "url": "",
  "title": "",
  "source_type": "official_importer|israeli_specs|israeli_review|price_list|global_fallback|unknown",
  "market_scope": "IL|EU|GLOBAL|UNKNOWN",
  "fields_supported": []
}}
"""


def build_retry_discovery_prompt(seed: VehicleModelSeed, market="IL") -> str:
    """Discovery prompt for retry attempts (attempt 2+).

    Extends the base prompt with an explicit instruction to either return at
    least one grounded candidate_variant OR a no_variants_reason from the
    allowed enum — never return an empty candidate_variants list without an
    explanation.
    """
    base = build_discovery_prompt(seed, market)
    retry_hint = (
        "\nIMPORTANT — RETRY ATTEMPT: The previous attempt returned no usable VehicleVariant records. "
        "You MUST either:\n"
        "1. return at least one grounded candidate_variant with real field data, OR\n"
        "2. return candidate_variants: [] with an explicit no_variants_reason chosen from this enum:\n"
        "   model_not_sold_in_market | no_reliable_sources_found | insufficient_grounded_data | "
        "duplicate_existing_variant_only | seed_out_of_scope | model_discontinued_before_market_period | "
        "source_conflict_unresolved | blocked_by_validation\n"
        "Do NOT return an empty candidate_variants list without a no_variants_reason.\n"
        "Example when no sources found:\n"
        '{"candidate_variants":[],"no_variants_reason":"no_reliable_sources_found"}\n'
    )
    return base + retry_hint


def build_verification_prompt(candidate_variants, sources) -> str:
    candidates_json = json.dumps(candidate_variants or [], ensure_ascii=False, indent=2)
    sources_json = json.dumps(sources or [], ensure_ascii=False, indent=2)
    return f"""JSON only. No prose. No markdown. No explanations outside JSON.
Reason max 120 chars.
CANDIDATE_VARIANTS_TO_VERIFY:
{candidates_json}
SOURCES:
{sources_json}
Verify each candidate against the provided sources only.
Return JSON only:
{{"variant_verifications":[{{"candidate_index":0,"field_verifications":{{"body_type":{{"value":"...","status":"verified|partial|conflict|unverified|unknown","confidence":"high|medium|low","sources_count":0,"source_ids":[],"used_in_compare":false,"reason":"max 120 chars"}},"seats":{{}},"engine":{{}},"transmission":{{}},"fuel_type":{{}},"drivetrain":{{}},"generation":{{}},"year_start":{{}},"year_end":{{}}}},"overall_status":"verified|partial|conflict|unverified|unknown","overall_confidence":"high|medium|low","blocked_fields":[],"notes":[]}}]}}
"""
