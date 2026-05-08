from core.schemas import VehicleModelSeed

def build_discovery_prompt(seed:VehicleModelSeed,market='IL')->str:
    return f"""Return strict JSON only. Research {seed.make} {seed.model} {seed.year_start}-{seed.year_end} in {market}. Prefer IL sources; fallback EU/global with market_scope. No guesses. Output keys: search_queries,sources,candidate_variants,conflicts,unresolved,unresolved_reason."""

def build_verification_prompt(candidate_variant,sources)->str:
    return "Verify each field against sources. Return strict JSON with field_verifications, overall_status, overall_confidence, blocked_fields, notes."
