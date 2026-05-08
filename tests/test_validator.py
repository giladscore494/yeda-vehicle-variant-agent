from tests.test_schema import _vf
from core.schemas import *
from core.validators import classify_variant, validate_variant

def _var():
    return VehicleVariant(variant_id='x',make='Kia',model='Sportage',aliases=[],year_start=2016,year_end=2021,market=Market.IL,generation=None,body_type=_vf('suv'),seats=_vf(5),engine=_vf('1.6','partial'),transmission=_vf('automatic','partial'),fuel_type=_vf('petrol','partial'),drivetrain=_vf('FWD','unknown'),trim=None,doors=None,verification_status=VerificationStatus.partial,confidence=Confidence.medium,sources_count=1,created_at='a',updated_at='a',notes=[])

def test_classifications():
    v=_var(); assert classify_variant(v)=='partial'; v.engine.status=VerificationStatus.conflict; assert classify_variant(v)=='conflict'

def test_verified_without_sources_fails():
    v=_var(); v.engine.status=VerificationStatus.verified; v.engine.sources_count=0
    ok, errs = validate_variant(v)
    assert not ok and 'verified_without_source' in errs

def test_used_in_compare_with_zero_sources_fails():
    v=_var(); v.drivetrain.sources_count=0; v.drivetrain.used_in_compare=True; v.drivetrain.status=VerificationStatus.unknown
    ok, errs = validate_variant(v)
    assert not ok and 'invalid_used_in_compare' in errs
