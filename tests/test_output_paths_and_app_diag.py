import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import storage.json_store as json_store


def test_get_output_paths_includes_batch_state():
    paths = json_store.get_output_paths()
    assert "batch_state" in paths
    assert str(paths["batch_state"]).endswith("data/output/batch_state.json")


def test_ensure_output_files_creates_batch_state_as_object(tmp_path, monkeypatch):
    monkeypatch.setattr(json_store, "project_root", lambda: tmp_path)

    json_store.ensure_output_files()

    batch_state_path = tmp_path / "data/output/batch_state.json"
    assert batch_state_path.exists()
    assert json.loads(batch_state_path.read_text(encoding="utf-8")) == {}


def test_app_compiles_when_batch_state_exists(tmp_path):
    app_path = Path(__file__).resolve().parents[1] / "app.py"
    compile(app_path.read_text(encoding="utf-8"), str(app_path), "exec")


def test_render_json_download_warns_when_missing(monkeypatch):
    import app

    warned = []
    downloaded = []

    monkeypatch.setattr(app.st, "warning", lambda msg: warned.append(msg))
    monkeypatch.setattr(app.st, "download_button", lambda *args, **kwargs: downloaded.append((args, kwargs)))

    app._render_json_download(None, "Download batch_state.json", "batch_state.json", "batch_state.json not found yet")

    assert warned == ["batch_state.json not found yet"]
    assert downloaded == []
