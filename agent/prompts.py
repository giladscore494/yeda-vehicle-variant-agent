from core.schemas import VehicleModelSeed

CRITICAL_FIELDS = [
    "body_type", "seats", "engine", "transmission", "fuel_type",
    "drivetrain", "generation", "year_start", "year_end"
]

def build_discovery_prompt(seed:VehicleModelSeed,market='IL')->str:
    return f"""Return strict JSON only. Research {seed.make} {seed.model} {seed.year_start}-{seed.year_end} in {market}.
Prefer Israeli sources in this order: official importer/manufacturer pages, Israeli spec/review sites, Israeli price/spec pages, archived Israeli pages, then EU/global only as fallback.
For every critical field, search for and cite at least 2 independent sources when possible.
Critical fields are: body_type, seats, engine, transmission, fuel_type, drivetrain, generation, year_start, year_end.
Do not collapse a long model range into one variant.
First identify generations and major facelift periods across the requested years.
Then identify major {market}-market variants per generation using: body_type, seats, engine, transmission, fuel_type, drivetrain, year_start, year_end, generation.
If exact {market}-market variants cannot be fully verified, create partial variants by generation rather than one generic all-years variant.
For model ranges longer than 8 years, return at least generation-level candidate variants unless no evidence exists.
Candidate variants must include all critical fields with null when unknown.
Every candidate must include field_sources or source_urls per field when available.
If fewer than 2 independent sources support a field, do not mark it as verified.
If only one source supports the field, mark it as partial unless the source is official/importer-level and directly supports the field.
If no source supports the field, mark it as unknown.
Do not use inferred, assumed, likely, typical, common, estimated, or known-configuration facts as verified facts.
No source means not verified. No conflicting source found is not evidence.
If only one generic candidate is returned for a model range longer than 8 years, add warning keys: range_collapsed=true and range_collapse_reason with explanation.
Output keys: search_queries,sources,candidate_variants,candidate_variants_count,conflicts,unresolved,unresolved_reason,field_evidence,range_collapsed,range_collapse_reason.
In field_evidence, include per field where possible: sources_count, insufficient_sources, source_urls, reason, status.
"""

def build_verification_prompt(candidate_variant,sources)->str:
    return """Verify each field against sources. Return strict JSON with field_verifications, overall_status, overall_confidence, blocked_fields, notes.
Always include all critical fields in field_verifications: body_type, seats, engine, transmission, fuel_type, drivetrain, generation, year_start, year_end.
Allowed statuses are only: verified, partial, conflict, unverified, unknown.
Do not output inferred, assumed, likely, typical, common, or estimated as a status.
If no direct source exists for a field: status='unknown', confidence='low', sources_count=0, used_in_compare=false.
Do not omit fields.
If a field is inferred but not directly source-backed, output: status='unknown', sources_count=0, used_in_compare=false, reason='Model inference without source is not accepted.'
"""
