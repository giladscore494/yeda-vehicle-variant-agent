import json
import os
from typing import Any

try:
    import streamlit as st
except Exception:
    st = None

IMPORT_ERROR = None
try:
    from google import genai
    from google.genai import types
except Exception as exc:
    genai = None
    types = None
    IMPORT_ERROR = str(exc)


def parse_json_from_gemini_text(raw_text: str) -> tuple[dict | list | None, str | None]:
    if raw_text is None or str(raw_text).strip() == "":
        return None, "empty raw_text"

    text = str(raw_text).lstrip("\ufeff").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    def _loads(candidate: str):
        parsed = json.loads(candidate)
        if isinstance(parsed, (dict, list)):
            return parsed, None
        return None, f"parsed JSON is {type(parsed).__name__}, expected dict/list"

    try:
        return _loads(text)
    except Exception as exc:
        direct_err = str(exc)

    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        try:
            unescaped = json.loads(text)
            if isinstance(unescaped, str):
                parsed, err = _loads(unescaped)
                if parsed is not None:
                    return parsed, None
        except Exception:
            pass

    first_obj, last_obj = text.find("{"), text.rfind("}")
    if first_obj != -1 and last_obj > first_obj:
        snippet = text[first_obj:last_obj + 1]
        try:
            return _loads(snippet)
        except Exception as exc:
            return None, f"failed to parse extracted object: {exc}; direct_error: {direct_err}"

    first_arr, last_arr = text.find("["), text.rfind("]")
    if first_arr != -1 and last_arr > first_arr:
        snippet = text[first_arr:last_arr + 1]
        try:
            return _loads(snippet)
        except Exception as exc:
            return None, f"failed to parse extracted array: {exc}; direct_error: {direct_err}"

    return None, f"failed to parse raw text: {direct_err}"


class GeminiClient:
    def __init__(self):
        secrets = getattr(st, "secrets", None) if st else None

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
        self.fast_model = (_secret_get("GEMINI_MODEL_FAST")) or os.getenv("GEMINI_MODEL_FAST", "gemini-3-flash-preview")
        self.strong_model = (_secret_get("GEMINI_MODEL_STRONG")) or os.getenv("GEMINI_MODEL_STRONG", "gemini-3-pro-preview")
        self.client = genai.Client(api_key=self.api_key) if genai is not None and self.api_key else None

    def has_api_key(self) -> bool:
        return bool(self.api_key)

    def get_api_key_source(self) -> str:
        if self._secrets_api_key:
            return "streamlit_secrets"
        if self._env_api_key:
            return "env"
        return "missing"

    def _response(self, *, model: str, grounding_requested: bool, request_attempted: bool, ok: bool, error: str = None, data: Any = None, raw_text: str = None, parsed_json: Any = None, parse_error: str = None):
        return {
            "ok": ok,
            "provider": "gemini",
            "model": model,
            "grounding_requested": grounding_requested,
            "request_attempted": request_attempted,
            "error": error,
            "data": data,
            "raw_text": raw_text,
            "parsed_json": parsed_json,
            "parse_error": parse_error,
        }

    def _validate_ready(self, model: str, grounding_requested: bool):
        if not self.has_api_key():
            return self._response(model=model, grounding_requested=grounding_requested, request_attempted=False, ok=False, error="GEMINI_API_KEY missing")
        if genai is None or types is None:
            return self._response(model=model, grounding_requested=grounding_requested, request_attempted=False, ok=False, error=f"google-genai import failed: {IMPORT_ERROR}")
        if self.client is None:
            return self._response(model=model, grounding_requested=grounding_requested, request_attempted=False, ok=False, error="Gemini client not initialized")
        return None

    def generate_json(self, prompt, schema_hint=None, strong=False, model_override=None):
        model = model_override or (self.strong_model if strong else self.fast_model)
        invalid = self._validate_ready(model, False)
        if invalid:
            return invalid
        try:
            response = self.client.models.generate_content(model=model, contents=prompt, config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.1))
            raw_text = getattr(response, "text", "") or ""
            parsed_json, parse_error = parse_json_from_gemini_text(raw_text)
            ok = parsed_json is not None and parse_error is None
            return self._response(model=model, grounding_requested=False, request_attempted=True, ok=ok, error=(None if ok else parse_error), data=(parsed_json if ok else None), raw_text=raw_text, parsed_json=(parsed_json if ok else None), parse_error=(None if ok else parse_error))
        except Exception as exc:
            return self._response(model=model, grounding_requested=False, request_attempted=True, ok=False, error=f"Gemini call failed: {exc}")

    def grounded_generate_json(self, prompt, schema_hint=None, strong=False, model_override=None):
        model = model_override or (self.strong_model if strong else self.fast_model)
        invalid = self._validate_ready(model, True)
        if invalid:
            return invalid
        try:
            config = types.GenerateContentConfig(tools=[types.Tool(google_search=types.GoogleSearch())], response_mime_type="application/json", temperature=0.1)
            response = self.client.models.generate_content(model=model, contents=prompt, config=config)
            raw_text = getattr(response, "text", "") or ""
            parsed_json, parse_error = parse_json_from_gemini_text(raw_text)
            ok = parsed_json is not None and parse_error is None
            return self._response(model=model, grounding_requested=True, request_attempted=True, ok=ok, error=(None if ok else parse_error), data=(parsed_json if ok else None), raw_text=raw_text, parsed_json=(parsed_json if ok else None), parse_error=(None if ok else parse_error))
        except Exception as exc:
            return self._response(model=model, grounding_requested=True, request_attempted=True, ok=False, error=f"Gemini grounding/search call failed: {exc}")
