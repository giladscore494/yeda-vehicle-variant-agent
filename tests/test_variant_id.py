from core.variant_id import generate_variant_id

def test_variant_id():
    x=generate_variant_id('Kia','Sportage',2016,2021,'IL','1.6 Turbo','Automatic','SUV')
    assert ' ' not in x and '__' not in x
    assert x==generate_variant_id('Kia','Sportage',2016,2021,'IL','1.6 Turbo','Automatic','SUV')
    assert 'unknown_engine' in generate_variant_id('Kia','Sportage',2016,2021,'IL',None,None,None)
