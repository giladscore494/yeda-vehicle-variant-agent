from tests.test_schema import _vf
from core.schemas import *
from core.validators import classify_variant

def _var():
    return VehicleVariant(variant_id='x',make='Kia',model='Sportage',aliases=[],year_start=2016,year_end=2021,market=Market.IL,generation=None,body_type=_vf('suv'),seats=_vf(5),engine=_vf('1.6','partial'),transmission=_vf('automatic','partial'),fuel_type=_vf('petrol','partial'),drivetrain=_vf('FWD','unknown'),trim=None,doors=None,verification_status=VerificationStatus.partial,confidence=Confidence.medium,sources_count=1,created_at='a',updated_at='a',notes=[])

def test_classifications():
    v=_var(); assert classify_variant(v)=='partial'; v.engine.status=VerificationStatus.conflict; assert classify_variant(v)=='conflict'
