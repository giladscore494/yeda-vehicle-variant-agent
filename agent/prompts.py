import json

from core.schemas import VehicleModelSeed


def build_discovery_prompt(seed: VehicleModelSeed, market="IL") -> str:
    return f"""JSON only. No prose. No markdown. No explanations outside JSON.
Reason max 120 chars. Evidence snippet max 220 chars. Max 2 snippets per source.
Research make={seed.make}, model={seed.model}, year_start={seed.year_start}, year_end={seed.year_end}, market={market}.
You must discover actual candidate variants for this make/model/year range.
Do not return empty candidate objects.
If no usable facts are found, return candidate_variants=[] and unresolved=true.
Return at most 12 candidates.
For long ranges, split by generation and major powertrain.
Do not collapse 2017-2026 into one candidate if generations/engines changed.
Candidate must have at least one usable identity field: engine, transmission, fuel_type, body_type, generation, year_start, or year_end.
If all critical fields are unknown/null, do not include the candidate.
Return exactly top-level keys: {{"search_queries":[],"sources":[],"candidate_variants":[],"conflicts":[],"unresolved":false,"unresolved_reason":null}}
Each candidate_variant must include exactly this structure:
{{"candidate_index":0,"make":"...","model":"...","year_start":2017,"year_end":2023,"generation":"optional string or null","body_type":"sedan|hatchback|suv|crossover|wagon|mpv|pickup|van|coupe|convertible|minivan|commercial|unknown|null","seats":5,"engine":"string or null","transmission":"manual|automatic|cvt|e_cvt|dual_clutch|single_speed_ev|unknown|null","fuel_type":"petrol|diesel|hybrid|plug_in_hybrid|electric|hydrogen|lpg|unknown|null","drivetrain":"FWD|RWD|AWD|4WD|null","trim":"string or null","source_urls":[],"field_sources":{{"body_type":[],"seats":[],"engine":[],"transmission":[],"fuel_type":[],"drivetrain":[],"generation":[],"year_start":[],"year_end":[]}},"notes":[]}}
Each source must include exactly this structure:
{{"source_id":"source_1","url":"...","title":"...","source_name":"...","source_type":"official_importer|israeli_specs|israeli_review|price_list|global_fallback|unknown","market_scope":"IL|EU|GLOBAL|UNKNOWN","fields_supported":[],"evidence_snippet":"max 220 chars"}}
Allowed statuses: verified, partial, conflict, unverified, unknown.
Forbidden statuses: inferred, assumed, likely, typical, common, estimated, guessed.
No prose outside JSON. No markdown. Use compact JSON only.
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
