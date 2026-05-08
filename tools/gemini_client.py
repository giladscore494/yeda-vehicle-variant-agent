import json
import os

try:
    import streamlit as st
except Exception:
    st = None


class GeminiClient:
    def __init__(self):
        secrets = getattr(st, "secrets", None) if st else None
        self._secrets = secrets

        def _secret_get(key):
            if secrets is None:
                return None
            try:
                return secrets.get(key)
            except Exception:
                return None

        self._secrets_api_key = _secret_get("GEMINI_API_KEY")
        self._env_api_key = os.getenv("GEMINI_API_KEY")
        self.api_key = self._secrets_api_key or self._env_api_key
        self.fast_model = (_secret_get("GEMINI_MODEL_FAST")) or os.getenv(
            "GEMINI_MODEL_FAST", "gemini-3-flash-preview"
        )
        self.strong_model = (_secret_get("GEMINI_MODEL_STRONG")) or os.getenv(
            "GEMINI_MODEL_STRONG", "gemini-3-pro-preview"
        )

    def has_api_key(self) -> bool:
        return bool(self.api_key)

    def get_api_key_source(self) -> str:
        if self._secrets_api_key:
            return "streamlit_secrets"
        if self._env_api_key:
            return "env"
        return "missing"

    def _detect_grounding_support(self):
        try:
            import google.genai  # noqa: F401

            return True
        except Exception:
            return None

    def get_config_status(self) -> dict:
        client_import_ok = False
        try:
            import google.genai  # noqa: F401

            client_import_ok = True
        except Exception:
            client_import_ok = False

        return {
            "has_api_key": self.has_api_key(),
            "api_key_source": self.get_api_key_source(),
            "fast_model": self.fast_model,
            "strong_model": self.strong_model,
            "client_import_ok": client_import_ok,
            "grounding_supported": self._detect_grounding_support(),
        }

    def _safe_parse(self, text: str):
        try:
            return json.loads(text)
        except Exception:
            try:
                start = text.find("{")
                end = text.rfind("}")
                if start >= 0 and end > start:
                    return json.loads(text[start : end + 1])
            except Exception:
                return None
        return None

    def _response(self, *, model: str, grounding_requested: bool, request_attempted: bool, ok: bool, error: str = None, data=None, raw_text: str = ""):
        return {
            "ok": ok,
            "provider": "gemini",
            "model": model,
            "grounding_requested": grounding_requested,
            "request_attempted": request_attempted,
            "error": error,
            "data": data,
            "raw_text": raw_text,
        }

    def generate_json(self, prompt, schema_hint=None, strong=False):
        model = self.strong_model if strong else self.fast_model
        if not self.has_api_key():
            return self._response(
                model=model,
                grounding_requested=False,
                request_attempted=False,
                ok=False,
                error="GEMINI_API_KEY missing",
                data=None,
            )

        request_attempted = True
        raw = '{"ok": false, "error": "Gemini client unavailable in local mock environment"}'
        parsed = self._safe_parse(raw)
        if parsed is None:
            parsed = self._safe_parse(raw)
        if parsed is None:
            return self._response(
                model=model,
                grounding_requested=False,
                request_attempted=request_attempted,
                ok=False,
                error="Invalid JSON returned from Gemini",
                data=None,
                raw_text=raw,
            )
        return self._response(
            model=model,
            grounding_requested=False,
            request_attempted=request_attempted,
            ok=bool(parsed.get("ok")),
            error=parsed.get("error"),
            data=parsed.get("data", parsed),
            raw_text=raw,
        )

    def grounded_generate_json(self, prompt, schema_hint=None, strong=False):
        model = self.strong_model if strong else self.fast_model
        if not self.has_api_key():
            return self._response(
                model=model,
                grounding_requested=True,
                request_attempted=False,
                ok=False,
                error="GEMINI_API_KEY missing",
                data=None,
            )

        request_attempted = True
        return self._response(
            model=model,
            grounding_requested=True,
            request_attempted=request_attempted,
            ok=False,
            error="Gemini grounding/search is not configured in this client yet",
            data=None,
        )
