import json

from core.schemas import VehicleModelSeed


def build_discovery_prompt(seed: VehicleModelSeed, market="IL") -> str:
    return f"""Return compact JSON only. No prose. No markdown.
No hidden reasoning.
Reason max 120 chars.
Evidence snippet max 220 chars.

Research make={seed.make}, model={seed.model}, year_start={seed.year_start}, year_end={seed.year_end}, market={market}.
Discover real candidate variants and split long ranges by generation and major powertrain.
Return at most 12 candidates.
Do not return empty candidate variants.
Each candidate must have at least one usable field value.
If no usable candidate data exists, return candidate_variants=[] and unresolved=true.

Required top-level JSON shape (exact keys):
{{
  "make": "...",
  "model": "...",
  "market": "...",
  "year_start": 0,
  "year_end": 0,
  "search_queries": [],
  "sources": [],
  "candidate_variants": [],
  "conflicts": [],
  "unresolved": false,
  "unresolved_reason": null,
  "data_quality": {{
    "usable_candidate_count": 0,
    "empty_candidate_count": 0,
    "warnings": []
  }}
}}

Each candidate_variant must include:
{{
  "candidate_index": 0,
  "generation": {{"value": "", "status": "verified|partial|conflict|unverified|unknown", "sources_count": 0, "source_urls": [], "reason": ""}},
  "year_start": {{"value": null, "status": "verified|partial|conflict|unverified|unknown", "sources_count": 0, "source_urls": [], "reason": ""}},
  "year_end": {{"value": null, "status": "verified|partial|conflict|unverified|unknown", "sources_count": 0, "source_urls": [], "reason": ""}},
  "body_type": {{"value": "", "status": "verified|partial|conflict|unverified|unknown", "sources_count": 0, "source_urls": [], "reason": ""}},
  "seats": {{"value": null, "status": "verified|partial|conflict|unverified|unknown", "sources_count": 0, "source_urls": [], "reason": ""}},
  "engine": {{"value": "", "status": "verified|partial|conflict|unverified|unknown", "sources_count": 0, "source_urls": [], "reason": ""}},
  "transmission": {{"value": "", "status": "verified|partial|conflict|unverified|unknown", "sources_count": 0, "source_urls": [], "reason": ""}},
  "fuel_type": {{"value": "", "status": "verified|partial|conflict|unverified|unknown", "sources_count": 0, "source_urls": [], "reason": ""}},
  "drivetrain": {{"value": "", "status": "verified|partial|conflict|unverified|unknown", "sources_count": 0, "source_urls": [], "reason": ""}},
  "trim": {{"value": "", "status": "verified|partial|conflict|unverified|unknown", "sources_count": 0, "source_urls": [], "reason": ""}}
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
Do not use outside knowledge.
Do not infer.
Do not guess.
If a candidate value is present but not supported by the provided sources:
- keep the value
- status=\"unverified\"
- sources_count=0
- used_in_compare=false
- reason=\"Candidate value not verified by provided sources.\"
If a field is missing:
- value=null
- status=\"unknown\"
- sources_count=0
- used_in_compare=false
Return JSON only:
{{"variant_verifications":[{{"candidate_index":0,"field_verifications":{{"body_type":{{"value":"...","status":"verified|partial|conflict|unverified|unknown","confidence":"high|medium|low","sources_count":0,"source_ids":[],"used_in_compare":false,"reason":"max 120 chars"}},"seats":{{}},"engine":{{}},"transmission":{{}},"fuel_type":{{}},"drivetrain":{{}},"generation":{{}},"year_start":{{}},"year_end":{{}}}},"overall_status":"verified|partial|conflict|unverified|unknown","overall_confidence":"high|medium|low","blocked_fields":[],"notes":[]}}]}}
Allowed statuses only: verified, partial, conflict, unverified, unknown.
Forbidden: inferred, assumed, likely, typical, common, estimated, guessed.
No prose. No markdown. Compact JSON only.
"""
