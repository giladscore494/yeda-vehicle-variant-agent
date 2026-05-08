from __future__ import annotations

import json
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.batch_runner import load_all_accumulated_variants
from core.final_export_builder import build_clean_final_export, is_mock_contaminated_variant
from storage.json_store import get_output_paths, load_json_list, load_json_object, save_json


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--allow-empty", action="store_true")
    args = parser.parse_args()
    paths = get_output_paths()
    loaded = load_all_accumulated_variants()
    verified, partial = loaded["verified"], loaded["partial"]
    sources = loaded["sources"] or load_json_list(paths["vehicle_sources"])
    final_export = build_clean_final_export(verified, partial, sources=sources)
    final_export.setdefault("audit", {})["inputs_loaded"] = loaded["inputs_loaded"]
    out_dir = Path("data/output")
    out_dir.mkdir(parents=True, exist_ok=True)
    save_json(out_dir / "combined_vehicle_variants_final_clean.json", final_export)
    save_json(out_dir / "final_export_quality_report.json", final_export.get("quality_gate", {}))
    total_variants = int(final_export.get("counts", {}).get("total_variants", 0) or 0)
    combined_clean = load_json_object(out_dir / "combined_vehicle_variants_final_clean.json")
    expected = len(combined_clean.get("variants", [])) if isinstance(combined_clean, dict) else 0
    if expected > 0 and total_variants < expected:
        print(f"WARNING: variants ({total_variants}) are lower than expected accumulated count ({expected}).", file=sys.stderr)
    payload = {
        "status": "ok",
        "variants": total_variants,
        "grade": final_export.get("quality_gate", {}).get("grade"),
        "inputs_loaded": loaded["inputs_loaded"],
        "mock_remaining": any(is_mock_contaminated_variant(v) for v in final_export.get("variants", [])),
    }
    print(json.dumps(payload, ensure_ascii=False))
    if total_variants == 0 and not args.allow_empty:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
