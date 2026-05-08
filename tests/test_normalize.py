from core.normalize import normalize_transmission, normalize_fuel_type, normalize_body_type

def test_norm():
    assert normalize_transmission('Auto').value=='automatic'
    assert normalize_transmission('DSG').value=='dual_clutch'
    assert normalize_transmission('e-CVT').value=='e_cvt'
    assert normalize_fuel_type('PHEV').value=='plug_in_hybrid'
    assert normalize_fuel_type('EV').value=='electric'
    assert normalize_fuel_type('gasoline').value=='petrol'
    assert normalize_body_type('jeep/suv').value=='suv'
