from core.schemas import VehicleModelSeed

def build_discovery_prompt(seed:VehicleModelSeed,market='IL')->str:
    return f"""Return strict JSON only. Research {seed.make} {seed.model} {seed.year_start}-{seed.year_end} in {market}.
Prefer Israeli sources in this order: official importer/manufacturer pages, Israeli spec/review sites, Israeli price/spec pages, archived Israeli pages, then EU/global only as fallback.
For every critical field, search for and cite at least 2 independent sources when possible.
Critical fields are: body_type, seats, engine, transmission, fuel_type, drivetrain, generation, and year range.
If fewer than 2 independent sources support a field, do not mark it as verified.
If only one source supports the field, mark it as partial unless the source is official/importer-level and directly supports the field.
If no source supports the field, mark it as unknown.
Do not use inferred, assumed, likely, typical, common, estimated, or known-configuration facts as verified facts.
No source means not verified. No conflicting source found is not evidence.
Output keys: search_queries,sources,candidate_variants,conflicts,unresolved,unresolved_reason,field_evidence.
In field_evidence, include per field where possible: sources_count, insufficient_sources, source_urls, reason, status.
"""

def build_verification_prompt(candidate_variant,sources)->str:
    return """Verify each field against sources. Return strict JSON with field_verifications, overall_status, overall_confidence, blocked_fields, notes.
Allowed statuses are only: verified, partial, conflict, unverified, unknown.
Do not output inferred, assumed, likely, typical, common, or estimated as a status.
If a field is inferred but not directly source-backed, output: status='unknown', sources_count=0, used_in_compare=false, reason='Model inference without source is not accepted.'
"""
