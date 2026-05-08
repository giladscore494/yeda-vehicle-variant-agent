from tools.gemini_client import GeminiClient


if __name__ == '__main__':
    cfg = GeminiClient().get_config_status()
    print(f"has_api_key={cfg['has_api_key']}")
    print(f"api_key_source={cfg['api_key_source']}")
    print(f"fast_model={cfg['fast_model']}")
    print(f"strong_model={cfg['strong_model']}")
    print(f"client_import_ok={cfg['client_import_ok']}")
    print(f"grounding_supported={cfg['grounding_supported']}")
