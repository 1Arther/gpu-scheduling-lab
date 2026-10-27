"""Verify saved round-four local artifacts without fitting or touching the cluster."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from gpu_lab.comparison import read_jsonl
from gpu_lab.crossgpu.data import audit
from gpu_lab.nvidia.model import NvidiaModel, metrics
from gpu_lab.run import sha256


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results",type=Path,default=ROOT/"results/offline-v4")
    args = parser.parse_args()
    out = args.results
    read = lambda name:json.loads((out/name).read_text(encoding="utf-8"))
    run,plan,freeze,manifests = (read(n) for n in ("run-manifest.json","frozen-plan.json","selection-frozen-before-test.json","split-manifests.json"))
    for relative,digest in run["source_sha256"].items():
        if sha256(ROOT/relative) != digest:
            raise ValueError(f"Source changed since fitting: {relative}")
    if sha256(out/"frozen-plan.json") != freeze["plan_sha256"]:
        raise ValueError("Frozen plan mismatch")
    historic = read("historical-results-preserved.json")
    for relative,digest in historic["sha256"].items():
        if sha256(ROOT/relative) != digest:
            raise ValueError(f"Historical result changed: {relative}")
    verified = {}
    for name,config in plan["protocols"].items():
        splits = {split:read_jsonl(out/name/f"{split}.jsonl") for split in ("train","validation","test")}
        audit(splits,model_disjoint=config["kind"] != "known_model_unseen_episode")
        if set(plan["fresh_test_models"]) & {r["device_id"] for r in splits["train"]+splits["validation"]}:
            raise ValueError("Fresh test model leaked into fitting/validation")
        actual_ids = {k:[r["sample_id"] for r in rows] for k,rows in splits.items()}
        if actual_ids != manifests[name]["sample_ids"]:
            raise ValueError("Split manifest sample membership mismatch")
        selected = out/name/"selected-model.joblib"
        if sha256(selected) != freeze["model_sha256"][name]:
            raise ValueError("Selected model changed after test selection freeze")
        candidates = freeze["validation_comparisons"][name]
        choice = max(candidates,key=lambda r:(r["validation_observed_macro_f1"],r["validation_accuracy"],-r["features"]))["feature_set"]
        if choice != freeze["selected_feature_sets"][name]:
            raise ValueError("Choice not reproducible from validation-only metrics")
        model = NvidiaModel.load(selected)
        saved = read(f"{name}/metrics.json")
        for split,rows in splits.items():
            reproduced = metrics(rows,model.raw_predict([r["features"] for r in rows]))
            if reproduced != saved["selected_metrics"][split]:
                raise ValueError(f"Non-reproducible selected metrics: {name}/{split}")
        verified[name] = {k:saved["selected_metrics"]["test"][k] for k in ("accuracy","macro_f1")}
    reg = read("simulation-regression.json")
    if len({r["trace_sha256"] for r in reg["summary"]}) != 1 or not reg["pod_label_checks"]:
        raise ValueError("Regression trace/label mismatch")
    if not all(r["completed"] == 60 and r["all_resources_restored"] and r["no_overallocation"] for r in reg["audit"].values()):
        raise ValueError("Simulation regression audit failure")
    print(json.dumps({"verified":True,"historical_files_unchanged":len(historic["sha256"]),"source_files":len(run["source_sha256"]),
                      "selected_model_metrics":verified,"test_used_for_selection":False},ensure_ascii=False,indent=2))


if __name__ == "__main__":
    main()
