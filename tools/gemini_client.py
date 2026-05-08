import json
import os

try:
    import streamlit as st
except Exception:
    st = None


class GeminiClient:
    def __init__(self):
        secrets = getattr(st, "secrets", None) if st else None
        self.api_key = (secrets.get("GEMINI_API_KEY") if secrets else None) or os.getenv("GEMINI_API_KEY")
        self.fast_model = (secrets.get("GEMINI_MODEL_FAST") if secrets else None) or os.getenv(
            "GEMINI_MODEL_FAST", "gemini-3-flash-preview"
        )
        self.strong_model = (secrets.get("GEMINI_MODEL_STRONG") if secrets else None) or os.getenv(
            "GEMINI_MODEL_STRONG", "gemini-3-pro-preview"
        )

    def has_api_key(self) -> bool:
        return bool(self.api_key)

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

    def generate_json(self, prompt, schema_hint=None, strong=False):
        if not self.has_api_key():
            return {"ok": False, "error": "GEMINI_API_KEY missing", "data": None}

        # Placeholder: SDK wiring intentionally omitted in this repo stage.
        raw = '{"ok": false, "error": "Gemini client unavailable in local mock environment"}'
        parsed = self._safe_parse(raw)
        if parsed is None:
            parsed = self._safe_parse(raw)  # single retry
        if parsed is None:
            return {"ok": False, "error": "Invalid JSON returned from Gemini", "data": None}
        return parsed

    def grounded_generate_json(self, prompt, schema_hint=None, strong=False):
        if not self.has_api_key():
            return {"ok": False, "error": "GEMINI_API_KEY missing", "data": None}
        return {
            "ok": False,
            "error": "Gemini grounding/search is not configured in this client yet",
            "data": None,
        }
