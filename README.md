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

## Batch Runner Resume Pipeline

- Batch Runner uses deterministic alphabetical ordering by make/model/year.
- Resume is persisted in `data/output/batch_state.json`.
- Use **Run next batch** to continue from last completed seed.
- No run-all button by design.
- If state is lost, use **Rebuild progress from output files**.

## Export

- Final dataset export defaults to verified + partial variants.
- Latest batch result export contains only last batch summary and results.
- Raw debug exports are optional.
