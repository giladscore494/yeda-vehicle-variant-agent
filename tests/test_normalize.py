from core.normalize import normalize_transmission, normalize_fuel_type, normalize_body_type, normalize_verification_status

def test_norm():
    assert normalize_transmission('Auto').value=='automatic'
    assert normalize_fuel_type('EV').value=='electric'
    assert normalize_body_type('jeep/suv').value=='suv'

def test_normalize_verification_status_mappings():
    assert normalize_verification_status('inferred')=='unknown'
    assert normalize_verification_status('assumed')=='unknown'
    assert normalize_verification_status('likely')=='unknown'
    assert normalize_verification_status('verified')=='verified'
