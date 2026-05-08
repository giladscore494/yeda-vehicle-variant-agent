from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.final_export_builder import build_clean_final_export
from storage.json_store import get_output_paths, load_json_list, save_json


def main() -> None:
    paths = get_output_paths()
    verified = load_json_list(paths["vehicle_variants_verified"])
    partial = load_json_list(paths["vehicle_variants_partial"])
    sources = load_json_list(paths["vehicle_sources"])
    final_export = build_clean_final_export(verified, partial, sources=sources)
    out_dir = Path("data/output")
    out_dir.mkdir(parents=True, exist_ok=True)
    save_json(out_dir / "combined_vehicle_variants_final_clean.json", final_export)
    save_json(out_dir / "final_export_quality_report.json", final_export.get("quality_gate", {}))
    print(json.dumps({"status": "ok", "variants": final_export.get("counts", {}).get("total_variants"), "grade": final_export.get("quality_gate", {}).get("grade")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
