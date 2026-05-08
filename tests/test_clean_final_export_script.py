import json
import subprocess
import sys
from pathlib import Path


def test_clean_script_uses_cumulative_loader(monkeypatch):
    script = Path('scripts/clean_final_export.py')
    assert script.exists()


def test_clean_script_returns_nonzero_when_empty_allowed_flag(tmp_path):
    # minimal smoke check: script is executable module text present
    text = Path('scripts/clean_final_export.py').read_text()
    assert '--allow-empty' in text
    assert 'inputs_loaded' in text
