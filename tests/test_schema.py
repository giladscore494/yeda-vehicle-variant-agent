import pytest
from core.schemas import *

def _vf(v,s='verified'):
    return VerifiedField(value=v,status=VerificationStatus(s),confidence=Confidence.medium,sources_count=1,source_ids=['s1'],used_in_compare=True)

def test_valid_variant():
    VehicleVariant(variant_id='x',make='Kia',model='Sportage',aliases=[],year_start=2016,year_end=2021,market=Market.IL,generation=None,body_type=_vf('suv'),seats=_vf(5),engine=_vf('1.6'),transmission=_vf('automatic'),fuel_type=_vf('petrol'),drivetrain=_vf('FWD'),trim=None,doors=None,verification_status=VerificationStatus.partial,confidence=Confidence.medium,sources_count=1,created_at='a',updated_at='a',notes=[])

def test_invalid_year():
    with pytest.raises(Exception):
        VehicleVariant(variant_id='x',make='Kia',model='Sportage',aliases=[],year_start=2022,year_end=2021,market=Market.IL,generation=None,body_type=_vf('suv'),seats=_vf(5),engine=_vf('1.6'),transmission=_vf('automatic'),fuel_type=_vf('petrol'),drivetrain=_vf('FWD'),trim=None,doors=None,verification_status=VerificationStatus.partial,confidence=Confidence.medium,sources_count=1,created_at='a',updated_at='a',notes=[])
