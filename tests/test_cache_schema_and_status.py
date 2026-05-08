from core.schemas import VerificationStatus
from agent.runner import CACHE_SCHEMA_VERSION


def test_cache_schema_version_constant():
    assert CACHE_SCHEMA_VERSION == 'vehicle_variant_agent_v2'


def test_inferred_not_allowed_final_status():
    assert 'inferred' not in {s.value for s in VerificationStatus}
