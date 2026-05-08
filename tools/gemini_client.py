import json, os
try:
    import streamlit as st
except Exception:
    st=None
class GeminiClient:
    def __init__(self):
        self.api_key=(st.secrets.get('GEMINI_API_KEY') if st else None) or os.getenv('GEMINI_API_KEY')
        self.fast_model=os.getenv('GEMINI_MODEL_FAST','gemini-3-flash-preview')
        self.strong_model=os.getenv('GEMINI_MODEL_STRONG','gemini-3-pro-preview')
    def has_api_key(self)->bool: return bool(self.api_key)
    def generate_json(self,prompt,schema_hint=None,strong=False):
        if not self.has_api_key(): return {'ok':False,'error':'GEMINI_API_KEY missing'}
        return {'ok':False,'error':'Gemini client unavailable in local mock environment','raw_text':''}
    def grounded_generate_json(self,prompt,schema_hint=None,strong=False):
        if not self.has_api_key(): return {'ok':False,'error':'GEMINI_API_KEY missing'}
        return {'ok':False,'error':'Gemini grounding/search is not configured in this client yet'}
