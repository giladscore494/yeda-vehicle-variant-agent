# Yeda Vehicle Variant Agent

Default enrichment uses **Gemini Pro only** (`GEMINI_MODEL_STRONG=gemini-3-pro-preview`) for one-time/periodic Israeli variants data building.

- Persistent outputs are saved as JSON files and should be reused by Yeda Rechev later.
- Gemini responses must be compact JSON only (no prose/markdown).
- Mock mode is testing-only.
- Keep cache enabled to avoid paying twice.
- Start with one model, then run small batches.

## Secrets
- `GEMINI_API_KEY="..."`
- `GEMINI_MODEL_STRONG="gemini-3-pro-preview"`
- `GEMINI_MODEL_FAST="gemini-3-flash-preview"`
