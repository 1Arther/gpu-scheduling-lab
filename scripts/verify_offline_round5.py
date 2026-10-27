"""Verify frozen runtime artifacts and historical integrity without refitting."""
import argparse
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from gpu_lab.run import sha256
from gpu_lab.runtime.bundle import DEFAULT_BUNDLE
from gpu_lab.runtime.service import SuitabilityService


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results",type=Path,default=ROOT/"results/offline-v5-final")
    args = parser.parse_args()
    out = args.results
    read = lambda file:json.loads((out/file).read_text(encoding="utf-8"))
    run = read("run-manifest.json")
    for relative,expected in run["source_sha256"].items():
        if sha256(ROOT/relative) != expected:
            raise ValueError(f"Runtime code changed: {relative}")
    history = read("historical-results-preserved.json")
    for relative,expected in history["sha256"].items():
        if sha256(ROOT/relative) != expected:
            raise ValueError(f"Historical result changed: {relative}")
    service = SuitabilityService()
    if service.model_error or service.version != run["model_version"]:
        raise ValueError("Frozen bundle/model version no longer valid")
    if sha256(DEFAULT_BUNDLE/"random_forest.joblib") != sha256(ROOT/"results/offline-v4/known_states/selected-model.joblib"):
        raise ValueError("Runtime model differs from frozen fourth-round model")
    fleet = read("gpu-list.json")
    initial = [service.classify_gpu(g) for g in fleet]
    if initial != read("initial-classifications.json"):
        raise ValueError("Initial classifications not reproducible")
    for name in ("training","inference","unlabelled"):
        actual = service.rank_gpus(ROOT/f"examples/runtime/{name}.yaml",fleet)
        if actual != read(f"demo-{name}.json"):
            raise ValueError(f"Non-reproducible ranking: {name}")
    for row in read("volcano-local-source-audit.json")["local_source_files"]:
        if sha256(ROOT.parent/row["path"]) != row["sha256"]:
            raise ValueError("Local Go audit source changed")
    reg = read("simulation-regression.json")
    if len({r["trace_sha256"] for r in reg["metrics"]}) != 1 or not all(r["completed"] == 60 and r["no_overallocation"] and r["all_resources_restored"] for r in reg["audit"].values()):
        raise ValueError("Simulation regression mismatch")
    suites = list(ET.parse(out/"pytest-results.xml").getroot().iter("testsuite"))
    tests = {k:sum(int(s.get(k,0)) for s in suites) for k in ("tests","failures","errors","skipped")}
    if tests["failures"] or tests["errors"]:
        raise ValueError("pytest failures")
    print(json.dumps({"verified":True,"model_version":service.version,"historical_files_unchanged":len(history["sha256"]),
                      "tests":tests,"runtime_refitting":False,"cluster_access":False},ensure_ascii=False,indent=2))


if __name__ == "__main__":
    main()
