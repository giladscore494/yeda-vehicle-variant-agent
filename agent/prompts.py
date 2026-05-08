import json

from core.schemas import VehicleModelSeed


def build_discovery_prompt(seed: VehicleModelSeed, market="IL") -> str:
    return f"""Return compact JSON only. JSON only. No prose. No markdown.
Reason max 120 chars for conflict/unresolved only.
Research make={seed.make}, model={seed.model}, year_start={seed.year_start}, year_end={seed.year_end}, market={market}.
Return max 8 candidate_variants and max 4 sources.
Each evidence_snippet <= 120 chars.
Do not include per-field status/confidence.
Do not include per-field reason unless conflict/unresolved and keep short.
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
  "source_urls": [],
  "field_sources": {{
    "body_type": [], "seats": [], "engine": [], "transmission": [], "fuel_type": [], "drivetrain": [], "generation": [], "year_start": [], "year_end": []
  }},
  "notes": []
}}
"""


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
