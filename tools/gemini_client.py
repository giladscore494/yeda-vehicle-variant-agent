import json
import os

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
        self.client = None
        if genai is not None and self.api_key:
            self.client = genai.Client(api_key=self.api_key)

    def has_api_key(self) -> bool:
        return bool(self.api_key)

    def get_api_key_source(self) -> str:
        if self._secrets_api_key:
            return "streamlit_secrets"
        if self._env_api_key:
            return "env"
        return "missing"

    def get_config_status(self) -> dict:
        return {
            "has_api_key": self.has_api_key(),
            "api_key_source": self.get_api_key_source(),
            "fast_model": self.fast_model,
            "strong_model": self.strong_model,
            "client_import_ok": genai is not None and types is not None,
            "client_ready": self.client is not None,
            "import_error": IMPORT_ERROR,
            "grounding_supported": (genai is not None and types is not None) if self.has_api_key() else None,
        }

    def _extract_response_metadata(self, response):
        md = {}
        for attr in ("finish_reason", "usage_metadata", "grounding_metadata", "source_metadata", "citation_metadata"):
            val = getattr(response, attr, None)
            if val is not None:
                md[attr] = val
        cand = getattr(response, "candidates", None)
        if cand:
            first = cand[0]
            fr = getattr(first, "finish_reason", None)
            gm = getattr(first, "grounding_metadata", None)
            if fr is not None:
                md["candidate_finish_reason"] = fr
            if gm is not None:
                md["candidate_grounding_metadata"] = gm
        return md

    def _response(self, *, model: str, grounding_requested: bool, request_attempted: bool, ok: bool, error: str = None, data=None, raw_text=None, parse_error=None, response_metadata=None):
        return {
            "ok": ok,
            "provider": "gemini",
            "model": model,
            "grounding_requested": grounding_requested,
            "request_attempted": request_attempted,
            "error": error,
            "data": data,
            "raw_text": raw_text,
            "parsed_json": data,
            "parse_error": parse_error,
            "response_metadata": response_metadata or {},
        }

    def _parse_or_repair(self, model: str, text: str):
        try:
            return json.loads(text), text, None
        except Exception:
            pass

        repair_prompt = (
            "Convert the following into strict JSON only, with no markdown and no explanations.\n"
            f"Content:\n{text}"
        )
        try:
            repair_response = self.client.models.generate_content(
                model=model,
                contents=repair_prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.1,
                ),
            )
            repair_text = getattr(repair_response, "text", "") or ""
        except Exception as exc:
            return None, text, f"Invalid JSON and repair failed: {exc}"
        try:
            return json.loads(repair_text), repair_text, None
        except Exception as exc:
            return None, repair_text, f"Invalid JSON returned from Gemini: {exc}"

    def generate_json(self, prompt, schema_hint=None, strong=False, model_override=None):
        model = model_override or (self.strong_model if strong else self.fast_model)
        if not self.has_api_key():
            return self._response(model=model, grounding_requested=False, request_attempted=False, ok=False, error="GEMINI_API_KEY missing", data=None)
        if genai is None or types is None:
            return self._response(model=model, grounding_requested=False, request_attempted=False, ok=False, error=f"google-genai import failed: {IMPORT_ERROR}", data=None)
        if self.client is None:
            return self._response(model=model, grounding_requested=False, request_attempted=False, ok=False, error="Gemini client not initialized", data=None)

        try:
            response = self.client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.1,
                ),
            )
            raw_text = getattr(response, "text", "") or ""
            data, parsed_text, parse_error = self._parse_or_repair(model, raw_text)
            response_metadata = self._extract_response_metadata(response)
            if parse_error:
                return self._response(model=model, grounding_requested=False, request_attempted=True, ok=False, error=parse_error, data=None, raw_text=raw_text, parse_error=parse_error, response_metadata=response_metadata)
            return self._response(model=model, grounding_requested=False, request_attempted=True, ok=True, error=None, data=data, raw_text=raw_text, parse_error=None, response_metadata=response_metadata)
        except Exception as exc:
            return self._response(model=model, grounding_requested=False, request_attempted=True, ok=False, error=f"Gemini call failed: {exc}", data=None, raw_text=None, parse_error=None)

    def grounded_generate_json(self, prompt, schema_hint=None, strong=False, model_override=None):
        model = model_override or (self.strong_model if strong else self.fast_model)
        if not self.has_api_key():
            return self._response(model=model, grounding_requested=True, request_attempted=False, ok=False, error="GEMINI_API_KEY missing", data=None)
        if genai is None or types is None:
            return self._response(model=model, grounding_requested=True, request_attempted=False, ok=False, error=f"google-genai import failed: {IMPORT_ERROR}", data=None)
        if self.client is None:
            return self._response(model=model, grounding_requested=True, request_attempted=False, ok=False, error="Gemini client not initialized", data=None)

        try:
            config = types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
                response_mime_type="application/json",
                temperature=0.1,
            )
            response = self.client.models.generate_content(
                model=model,
                contents=prompt,
                config=config,
            )
            raw_text = getattr(response, "text", "") or ""
            data, parsed_text, parse_error = self._parse_or_repair(model, raw_text)
            response_metadata = self._extract_response_metadata(response)
            if parse_error:
                return self._response(model=model, grounding_requested=True, request_attempted=True, ok=False, error=parse_error, data=None, raw_text=raw_text, parse_error=parse_error, response_metadata=response_metadata)
            return self._response(model=model, grounding_requested=True, request_attempted=True, ok=True, error=None, data=data, raw_text=raw_text, parse_error=None, response_metadata=response_metadata)
        except Exception as exc:
            return self._response(model=model, grounding_requested=True, request_attempted=True, ok=False, error=f"Gemini grounding/search call failed: {exc}", data=None, raw_text=None, parse_error=None)
