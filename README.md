# Yeda Vehicle Variant Agent

## What this tool does
Builds/validates vehicle variants from `data/input/car_models_dict.py` into JSON outputs and provides a Streamlit dashboard.

## Why it exists
To support internal Yeda Rechev variant curation with auditable evidence and mock-first operation.

## Input file
- `data/input/car_models_dict.py`

## Output files
- `data/output/vehicle_variants_verified.json`
- `data/output/vehicle_variants_partial.json`
- `data/output/vehicle_conflicts.json`
- `data/output/vehicle_sources.json`
- `data/output/unresolved_models.json`
- `data/output/run_history.json`

## Local setup
```bash
pip install -r requirements.txt
streamlit run app.py
```

## Environment variables
- `GEMINI_API_KEY`
- `GEMINI_MODEL_FAST`
- `GEMINI_MODEL_STRONG`

## Streamlit Cloud deployment
1. Push repo to GitHub.
2. Open Streamlit Community Cloud.
3. Create app and select this repo.
4. Main file: `app.py`.
5. Add secret: `GEMINI_API_KEY = "..."`.
6. Deploy.

## Mock mode
App works without Gemini key and can run mock Kia Sportage generation.

## How to export to Yeda Rechev
Use Export tab and download lightweight Yeda JSON.

## Important warning
Gemini is not source of truth. Review output before production; only verified fields should drive compare scoring.
