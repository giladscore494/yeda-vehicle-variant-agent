from agent.runner import run_single_model
from core.schemas import Confidence, Market, VehicleVariant, VerificationStatus
from core.validators import classify_variant
from storage.json_store import get_output_paths, load_json_list
from core.schemas import VerifiedField


def _variant(**overrides):
    base = VehicleVariant(
        variant_id='abarth-500-test',
        make='Abarth',
        model='500',
        aliases=[],
        year_start=2018,
        year_end=2023,
        market=Market.IL,
        generation='312',
        body_type=VerifiedField(value='hatchback', status=VerificationStatus.partial, confidence=Confidence.medium, sources_count=1, source_ids=['s1'], used_in_compare=True),
        seats=VerifiedField(value=4, status=VerificationStatus.partial, confidence=Confidence.medium, sources_count=1, source_ids=['s1'], used_in_compare=True),
        engine=VerifiedField(value='1.4 Turbo', status=VerificationStatus.verified, confidence=Confidence.high, sources_count=2, source_ids=['s1','s2'], used_in_compare=True),
        transmission=VerifiedField(value='manual', status=VerificationStatus.verified, confidence=Confidence.high, sources_count=2, source_ids=['s1','s2'], used_in_compare=True),
        fuel_type=VerifiedField(value='petrol', status=VerificationStatus.verified, confidence=Confidence.high, sources_count=2, source_ids=['s1','s2'], used_in_compare=True),
        drivetrain=VerifiedField(value='FWD', status=VerificationStatus.partial, confidence=Confidence.medium, sources_count=1, source_ids=['s1'], used_in_compare=True),
        trim=VerifiedField(value='Base', status=VerificationStatus.unverified, confidence=Confidence.low, sources_count=0, source_ids=[], used_in_compare=False),
        doors=None,
        verification_status=VerificationStatus.partial,
        confidence=Confidence.medium,
        sources_count=3,
        created_at='a',
        updated_at='a',
        notes=[],
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


def test_engine_fuel_verified_and_body_partial_not_unresolved():
    v = _variant(seats=VerifiedField(value=4, status=VerificationStatus.unverified, confidence=Confidence.low, sources_count=0, source_ids=[], used_in_compare=False), transmission=VerifiedField(value='AT', status=VerificationStatus.unverified, confidence=Confidence.low, sources_count=0, source_ids=[], used_in_compare=False))
    assert classify_variant(v) in {'partial', 'verified'}


def test_useful_identity_with_one_sourced_field_is_partial():
    v = _variant(
        body_type=VerifiedField(value=None, status=VerificationStatus.unknown, confidence=Confidence.low, sources_count=0, source_ids=[], used_in_compare=False),
        seats=VerifiedField(value=None, status=VerificationStatus.unknown, confidence=Confidence.low, sources_count=0, source_ids=[], used_in_compare=False),
        transmission=VerifiedField(value=None, status=VerificationStatus.unknown, confidence=Confidence.low, sources_count=0, source_ids=[], used_in_compare=False),
        drivetrain=VerifiedField(value=None, status=VerificationStatus.unknown, confidence=Confidence.low, sources_count=0, source_ids=[], used_in_compare=False),
        fuel_type=VerifiedField(value='petrol', status=VerificationStatus.partial, confidence=Confidence.medium, sources_count=1, source_ids=['s1'], used_in_compare=True),
        engine=VerifiedField(value='1.4 turbo', status=VerificationStatus.unverified, confidence=Confidence.low, sources_count=0, source_ids=[], used_in_compare=False),
    )
    assert classify_variant(v) == 'partial'


def test_all_unknown_zero_sources_is_unresolved():
    v = _variant(
        generation='',
        body_type=VerifiedField(value=None, status=VerificationStatus.unknown, confidence=Confidence.low, sources_count=0, source_ids=[], used_in_compare=False),
        seats=VerifiedField(value=None, status=VerificationStatus.unknown, confidence=Confidence.low, sources_count=0, source_ids=[], used_in_compare=False),
        transmission=VerifiedField(value=None, status=VerificationStatus.unknown, confidence=Confidence.low, sources_count=0, source_ids=[], used_in_compare=False),
        drivetrain=VerifiedField(value=None, status=VerificationStatus.unknown, confidence=Confidence.low, sources_count=0, source_ids=[], used_in_compare=False),
        fuel_type=VerifiedField(value=None, status=VerificationStatus.unknown, confidence=Confidence.low, sources_count=0, source_ids=[], used_in_compare=False),
        engine=VerifiedField(value=None, status=VerificationStatus.unknown, confidence=Confidence.low, sources_count=0, source_ids=[], used_in_compare=False),
    )
    assert classify_variant(v) == 'unresolved'


def test_partial_variants_saved_to_partial_file(monkeypatch):
    cands = [
        {'engine': {'value': '1.4 Turbo', 'status': 'verified', 'sources_count': 2}, 'fuel_type': {'value': 'petrol', 'status': 'verified', 'sources_count': 2}, 'body_type': {'value': 'hatchback', 'status': 'partial', 'sources_count': 1}, 'drivetrain': {'value': 'FWD', 'status': 'partial', 'sources_count': 1}},
    ]
    monkeypatch.setattr('agent.runner.run_discovery', lambda *a, **k: {'ok': True, 'data': {'sources': [{'source_id': 's1'}], 'candidate_variants': cands}})
    out = run_single_model('Abarth', '500', 2018, 2023, model_mode='strong', force_refresh=True)
    assert out['partial_count'] >= 1
    partial = load_json_list(get_output_paths()['vehicle_variants_partial'])
    ids = {r.get('variant_id') for r in partial if isinstance(r, dict)}
    assert any(i and i.startswith('abarth_500') for i in ids)


def test_abarth_500_like_three_candidates_saved_as_partial(monkeypatch):
    cands = [
        {'engine': {'value': '1.4 Turbo 145', 'status': 'verified', 'sources_count': 2}, 'transmission': {'value': 'manual', 'status': 'verified', 'sources_count': 2}, 'fuel_type': {'value': 'petrol', 'status': 'verified', 'sources_count': 2}, 'body_type': {'value': 'hatchback', 'status': 'partial', 'sources_count': 1}},
        {'engine': {'value': '1.4 Turbo 165', 'status': 'verified', 'sources_count': 2}, 'transmission': {'value': 'manual', 'status': 'verified', 'sources_count': 2}, 'fuel_type': {'value': 'petrol', 'status': 'verified', 'sources_count': 2}, 'body_type': {'value': 'hatchback', 'status': 'partial', 'sources_count': 1}},
        {'engine': {'value': '1.4 Turbo 180', 'status': 'verified', 'sources_count': 2}, 'transmission': {'value': 'manual', 'status': 'verified', 'sources_count': 2}, 'fuel_type': {'value': 'petrol', 'status': 'verified', 'sources_count': 2}, 'body_type': {'value': 'hatchback', 'status': 'partial', 'sources_count': 1}},
    ]
    monkeypatch.setattr('agent.runner.run_discovery', lambda *a, **k: {'ok': True, 'data': {'sources': [{'source_id': 's1'}], 'candidate_variants': cands}})
    out = run_single_model('Abarth', '500', 2018, 2023, model_mode='strong', force_refresh=True)
    assert out['variants_created'] >= 3
    assert out['partial_count'] >= 3
    assert out['unresolved_count'] == 0


def test_abarth_600e_like_saved_partial_when_some_fields_unverified(monkeypatch):
    cands = [
        {'engine': {'value': 'electric 115kW', 'status': 'verified', 'sources_count': 2}, 'fuel_type': {'value': 'electric', 'status': 'verified', 'sources_count': 2}, 'body_type': {'value': 'crossover', 'status': 'partial', 'sources_count': 1}, 'drivetrain': {'value': 'FWD', 'status': 'partial', 'sources_count': 1}, 'seats': {'value': 5, 'status': 'unverified', 'sources_count': 0}, 'transmission': {'value': None, 'status': 'unknown', 'sources_count': 0}},
        {'engine': {'value': 'electric 176kW', 'status': 'verified', 'sources_count': 2}, 'fuel_type': {'value': 'electric', 'status': 'verified', 'sources_count': 2}, 'body_type': {'value': 'crossover', 'status': 'partial', 'sources_count': 1}, 'drivetrain': {'value': 'FWD', 'status': 'partial', 'sources_count': 1}, 'seats': {'value': 5, 'status': 'unverified', 'sources_count': 0}, 'transmission': {'value': None, 'status': 'unknown', 'sources_count': 0}},
    ]
    monkeypatch.setattr('agent.runner.run_discovery', lambda *a, **k: {'ok': True, 'data': {'sources': [{'source_id': 's1'}], 'candidate_variants': cands}})
    out = run_single_model('Abarth', '600e', 2024, 2026, model_mode='strong', force_refresh=True)
    assert out['partial_count'] >= 2
    assert out['unresolved_count'] == 0
