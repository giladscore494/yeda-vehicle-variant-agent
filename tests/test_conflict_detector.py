from core.conflict_detector import detect_conflicts
from tests.test_validator import _var

def test_conflicts():
    a=_var(); b=_var(); b.seats.value=7; b.engine.value='2.0'; c=detect_conflicts([a,b]); names={x.field_name for x in c}; assert 'seats' in names and 'engine' in names
