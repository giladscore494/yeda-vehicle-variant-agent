# Yeda Vehicle Variant Agent

## 1. What this tool does
This tool ingests a vehicle model dictionary, runs an agent pipeline (Gemini-backed or mock mode), classifies output variants, tracks trace history, and provides a Streamlit dashboard for inspection/export.

## 2. Why it exists
It supports Yeda Rechev vehicle-variant data curation with auditable JSON outputs, strict verification rules, and safe mock operation when no API key is configured.

## 3. Input
- `data/input/car_models_dict.py`

## 4. Output files
- `data/output/vehicle_variants_verified.json`
- `data/output/vehicle_variants_partial.json`
- `data/output/vehicle_conflicts.json`
- `data/output/vehicle_sources.json`
- `data/output/unresolved_models.json`
- `data/output/run_history.json`

## 5. Local setup
```bash
pip install -r requirements.txt
streamlit run app.py
```

## 6. Streamlit Community Cloud deployment
1. Push this repository to GitHub.
2. Open Streamlit Community Cloud.
3. Click **New app**.
4. Select your repository.
5. Choose branch **main**.
6. Set main file path to **app.py**.
7. Click **Deploy**.

## 7. Secrets
Add the following in Streamlit app settings → **Secrets**:

```toml
GEMINI_API_KEY = "your_key_here"
GEMINI_MODEL_FAST = "gemini-3-flash-preview"
GEMINI_MODEL_STRONG = "gemini-3-pro-preview"
```

## 8. Mock mode
The app works without a Gemini key. If `GEMINI_API_KEY` is missing, mock mode can still run and produce output files.

## 9. How to export to Yeda Rechev
Open the **Export** tab, click **Download Yeda Rechev Export JSON**, and copy the file into the destination app data folder.

## 10. Warning
Gemini is not source of truth. Review data before production. Only verified fields should enter compare scoring.
