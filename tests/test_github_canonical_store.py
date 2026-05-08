import json

from storage import github_canonical_store


class _Resp:
    def __init__(self, status_code: int, payload: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text
        self.content = b"1"

    def json(self):
        return self._payload


def test_github_contents_large_file_download_url_fallback(monkeypatch):
    pkg = {
        "schema_version": "resume_package_v1",
        "variants": [{"variant_id": f"v-{i}"} for i in range(263)],
        "batch_state": {"processed_seed_ids": [f"s-{i}" for i in range(59)], "next_seed_id": "audi__rs6__2008__2026__il"},
    }

    monkeypatch.setattr(
        github_canonical_store,
        "get_github_config",
        lambda: {
            "token": "github_pat_123456789012345678901234567890",
            "repo": "owner/repo",
            "branch": "main",
            "canonical_path": "data/canonical/resume_package_canonical.json",
            "backup_path": "data/canonical/resume_package_backup_previous.json",
        },
    )

    def _get(url, headers=None, params=None, timeout=30):
        if "api.github.com/repos/owner/repo/contents/" in url:
            return _Resp(
                200,
                {
                    "sha": "abc123",
                    "size": 2220403,
                    "encoding": "base64",
                    "content": "",
                    "download_url": "https://example.com/raw.json",
                },
            )
        if url == "https://example.com/raw.json":
            return _Resp(200, {}, json.dumps(pkg))
        return _Resp(404, {"message": "Not Found"})

    monkeypatch.setattr(github_canonical_store.requests, "get", _get)
    result = github_canonical_store.fetch_file_from_github("data/canonical/resume_package_canonical.json")
    assert isinstance(result, dict)
    assert len(result.get("variants", [])) == 263
