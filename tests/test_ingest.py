from core.ingest import parse_model_entry

def test_parse_corolla():
    s=parse_model_entry('Toyota','Corolla (1992-2026)')[0]; assert s.model=='Corolla' and s.year_start==1992

def test_parse_multi_range():
    s=parse_model_entry('Honda','Insight (2009-2014, 2018-2022)'); assert len(s)==2

def test_aliases():
    s=parse_model_entry('Honda','Jazz / Fit (2001-2026)')[0]; assert s.aliases==['Fit']
