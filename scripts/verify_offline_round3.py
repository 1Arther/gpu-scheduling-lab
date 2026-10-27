"""Read-only verification of third-round local artifacts; no network or GPU."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gpu_lab.comparison import read_jsonl
from gpu_lab.crossgpu.data import audit
from gpu_lab.crossgpu.model import CrossGPUModel, metrics
from gpu_lab.run import sha256


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, default=ROOT / "results/offline-v3-release")
    args = parser.parse_args()
    result = args.results
    manifest = json.loads((result / "run-manifest.json").read_text(encoding="utf-8"))
    for relative, digest in manifest["source_sha256"].items():
        if sha256(ROOT / relative) != digest:
            raise ValueError(f"Source changed since run: {relative}")
    history = json.loads((result / "historical-results-preserved.json").read_text(encoding="utf-8"))
    for relative, digest in history["sha256"].items():
        if sha256(ROOT / relative) != digest:
            raise ValueError(f"Historical artifact changed: {relative}")
    verified = {}
    for protocol in ("A-known-models", "B-unseen-models"):
        splits = {name: read_jsonl(result / f"{protocol}-{name}.jsonl") for name in ("train", "validation", "test")}
        audit(splits, model_disjoint=protocol.startswith("B"))
        model = CrossGPUModel.load(result / f"{protocol}-random-forest.joblib")
        saved = json.loads((result / f"{protocol}-metrics.json").read_text(encoding="utf-8"))
        for name, rows in splits.items():
            predictions = model.raw_predict([r["features"] for r in rows])
            if metrics(rows, predictions) != saved[name]:
                raise ValueError(f"Metrics not reproducible: {protocol}/{name}")
        verified[protocol] = {"accuracy": saved["test"]["accuracy"], "macro_f1": saved["test"]["macro_f1"]}
    regression = json.loads((result / "simulation-regression.json").read_text(encoding="utf-8"))
    if len({r["trace_sha256"] for r in regression["summary"]}) != 1:
        raise ValueError("Regression trace mismatch")
    for row in regression["audit"].values():
        if not row["no_overallocation"] or not row["all_resources_restored"] or row["completed"] != 60:
            raise ValueError("Regression audit failed")
    print(json.dumps({"verified": True, "source_files": len(manifest["source_sha256"]),
                      "historical_files_unchanged": len(history["sha256"]), "metrics": verified}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
