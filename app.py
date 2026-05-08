from __future__ import annotations

import os
from datetime import datetime, timezone

import streamlit as st


def has_gemini_key() -> bool:
    try:
        secret_key = st.secrets.get("GEMINI_API_KEY", "")
    except Exception:
        secret_key = ""
    env_key = os.getenv("GEMINI_API_KEY", "")
    return bool(secret_key or env_key)


def mock_demo_payload() -> dict:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "run_id": "mock_kia_sportage_2016_2021_il",
        "status": "mock_completed",
        "market": "IL",
        "created_at": now,
        "candidate_variants": [
            {
                "make": "Kia",
                "model": "Sportage",
                "year_start": 2016,
                "year_end": 2021,
                "market": "IL",
                "body_type": {"value": "suv", "status": "verified", "confidence": "high"},
                "seats": {"value": 5, "status": "verified", "confidence": "high"},
                "engine": {
                    "value": "1.6T / 2.0L",
                    "status": "partial",
                    "confidence": "medium",
                    "reason": "Multiple engines by trim",
                },
                "transmission": {
                    "value": "automatic / dual_clutch",
                    "status": "partial",
                    "confidence": "medium",
                    "reason": "Trim-dependent",
                },
                "fuel_type": {"value": "petrol", "status": "verified", "confidence": "medium"},
                "drivetrain": {"value": "fwd", "status": "partial", "confidence": "low"},
                "sources": [
                    {
                        "url": "https://example.com/mock-importer-sportage",
                        "source_type": "mock",
                        "market_scope": "IL",
                    }
                ],
            }
        ],
    }


def main() -> None:
    st.set_page_config(page_title="Yeda Vehicle Variant Agent", layout="wide")
    st.title("Yeda Vehicle Variant Agent")

    gemini_ready = has_gemini_key()
    if gemini_ready:
        st.success("Gemini API key found. Real mode can be enabled in full implementation.")
    else:
        st.warning("Gemini API key missing. Running in mock mode.")

    st.subheader("Run Single Model (Mock Demo)")
    if st.button("Run Mock Demo"):
        st.json(mock_demo_payload())


if __name__ == "__main__":
    main()
