import base64
import json

from agent import batch_runner


class _Resp:
    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


def _make_pkg(variants_count: int = 263, processed_count: int = 59, next_seed: str = "audi__rs6__2008__2026__il"):
    return {
        "schema_version": "resume_package_v1",
        "variants": [{"variant_id": f"v-{i}"} for i in range(variants_count)],
        "batch_state": {
            "processed_seed_ids": [f"s-{i}" for i in range(processed_count)],
            "last_completed_seed_id": "audi__rs5__2010__2026__il",
            "next_seed_id": next_seed,
        },
    }


def _mock_github(monkeypatch, repo_status=200, branch_status=200, contents_status=200, github_pkg=None):
    github_pkg = github_pkg or _make_pkg()
    encoded = base64.b64encode(json.dumps(github_pkg).encode("utf-8")).decode("utf-8")

    def _get(url, headers=None, timeout=30):
        if "/branches/" in url:
            payload = {"name": "main"} if branch_status == 200 else {"message": "Not Found"}
            return _Resp(branch_status, payload)
        if "/contents/" in url:
            payload = (
                {"content": encoded, "sha": "abc123", "size": 100, "download_url": "https://example.com/file.json"}
                if contents_status == 200
                else {"message": "Not Found" if contents_status == 404 else "Forbidden"}
            )
            return _Resp(contents_status, payload)
        payload = {"private": True, "default_branch": "main"} if repo_status == 200 else {"message": "Not Found" if repo_status == 404 else "Bad credentials"}
        return _Resp(repo_status, payload)

    monkeypatch.setattr(batch_runner.requests, "get", _get)


def _setup_common(monkeypatch, tmp_path, token="github_pat_123456789012345678901234567890"):
    monkeypatch.setattr(
        batch_runner,
        "get_github_config",
        lambda: {
            "token": token,
            "repo": "owner/repo",
            "branch": "main",
            "canonical_path": "data/canonical/resume_package_canonical.json",
            "backup_path": "data/canonical/resume_package_backup_previous.json",
        },
    )
    monkeypatch.setattr(batch_runner, "project_root", lambda: tmp_path)
    monkeypatch.setattr(
        batch_runner,
        "get_ordered_seed_list",
        lambda market="IL": [
            {"seed_id": "audi__rs5__2010__2026__il"},
            {"seed_id": "audi__rs6__2008__2026__il"},
            {"seed_id": "audi__rs7__2013__2026__il"},
        ],
    )
    monkeypatch.setattr(batch_runner, "load_imported_accumulated_variants", lambda: [])
    monkeypatch.setattr(batch_runner, "build_final_export", lambda: {"variants": [{"variant_id": f"x-{i}"} for i in range(263)]})
    monkeypatch.setattr(batch_runner, "build_resume_package", lambda: _make_pkg(variants_count=263))


def _write_local_canonical(tmp_path, pkg=None):
    pkg = pkg or _make_pkg()
    path = tmp_path / "data/canonical/resume_package_canonical.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(pkg), encoding="utf-8")


def test_diagnose_missing_token(monkeypatch, tmp_path):
    _setup_common(monkeypatch, tmp_path, token="")
    _mock_github(monkeypatch, repo_status=200, branch_status=200, contents_status=200)
    result = batch_runner.diagnose_canonical_github_sync()
    assert result["final_diagnosis"] == "GITHUB_TOKEN is missing or empty in Streamlit Secrets."
    assert result["single_root_cause"] == "GITHUB_TOKEN is missing or empty in Streamlit Secrets."


def test_diagnose_wrong_repo_404(monkeypatch, tmp_path):
    _setup_common(monkeypatch, tmp_path)
    _mock_github(monkeypatch, repo_status=404)
    result = batch_runner.diagnose_canonical_github_sync()
    assert result["single_root_cause"] == "GITHUB_REPO is wrong or token has no access to this repo."


def test_diagnose_wrong_branch_404(monkeypatch, tmp_path):
    _setup_common(monkeypatch, tmp_path)
    _mock_github(monkeypatch, repo_status=200, branch_status=404)
    result = batch_runner.diagnose_canonical_github_sync()
    assert result["single_root_cause"] == "GITHUB_BRANCH does not exist or is not accessible."


def test_diagnose_contents_404_with_local_valid(monkeypatch, tmp_path):
    _setup_common(monkeypatch, tmp_path)
    _write_local_canonical(tmp_path, _make_pkg(263, 59, "audi__rs6__2008__2026__il"))
    _mock_github(monkeypatch, repo_status=200, branch_status=200, contents_status=404)
    result = batch_runner.diagnose_canonical_github_sync()
    assert result["final_diagnosis"] == "Canonical file is missing at configured path on GitHub, but local canonical exists and should be usable. Push should create the file."
    assert any("GET /repos returned 200" in item for item in result["ruled_out"])
    assert any("GET /branches/main returned 200" in item for item in result["ruled_out"])


def test_diagnose_manual_push_rebuild_shrink(monkeypatch, tmp_path):
    _setup_common(monkeypatch, tmp_path)
    _write_local_canonical(tmp_path, _make_pkg(263, 59, "audi__rs6__2008__2026__il"))
    _mock_github(monkeypatch, repo_status=200, branch_status=200, contents_status=200, github_pkg=_make_pkg(263, 59, "audi__rs6__2008__2026__il"))
    monkeypatch.setattr(batch_runner, "build_final_export", lambda: {"variants": [{"variant_id": f"b-{i}"} for i in range(116)]})

    def _raise_shrink():
        raise ValueError("Accumulated export shrink detected. Refusing to generate resume package.")

    monkeypatch.setattr(batch_runner, "build_resume_package", _raise_shrink)
    result = batch_runner.diagnose_canonical_github_sync()
    assert result["final_diagnosis"] == "Manual push rebuilds from incomplete local outputs instead of pushing the valid local canonical."
    assert result["checks"]["shrink_guard_diagnosis"]["shrink_detected"] is True


def test_diagnose_valid_github_canonical_263(monkeypatch, tmp_path):
    _setup_common(monkeypatch, tmp_path)
    _write_local_canonical(tmp_path, _make_pkg(263, 59, "audi__rs6__2008__2026__il"))
    _mock_github(monkeypatch, repo_status=200, branch_status=200, contents_status=200, github_pkg=_make_pkg(263, 59, "audi__rs6__2008__2026__il"))
    result = batch_runner.diagnose_canonical_github_sync()
    assert result["single_root_cause"] == "No blocking root cause detected."
    assert result["checks"]["github_contents_check"]["github_variant_count"] == 263
    assert result["safe_to_continue_batch"] is True


def test_diagnostic_does_not_expose_token(monkeypatch, tmp_path):
    secret_token = "github_pat_SUPER_SECRET_TOKEN_12345678901234567890"
    _setup_common(monkeypatch, tmp_path, token=secret_token)
    _mock_github(monkeypatch, repo_status=200, branch_status=200, contents_status=200)
    result = batch_runner.diagnose_canonical_github_sync()
    assert secret_token not in json.dumps(result, ensure_ascii=False)
    assert result["checks"]["secrets"]["token_prefix_type"] == "github_pat"


def test_local_canonical_valid_is_safe_to_continue_batch(monkeypatch, tmp_path):
    _setup_common(monkeypatch, tmp_path)
    _write_local_canonical(tmp_path, _make_pkg(263, 59, "audi__rs6__2008__2026__il"))
    _mock_github(monkeypatch, repo_status=200, branch_status=200, contents_status=404)
    result = batch_runner.diagnose_canonical_github_sync()
    assert result["checks"]["local_canonical"]["local_expected_file"] is True
    assert result["safe_to_continue_batch"] is True
