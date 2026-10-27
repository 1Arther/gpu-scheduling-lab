"""Freeze and load already-fitted local artifacts. Never calls fit()."""
import json
from pathlib import Path
import shutil

from ..profiles import ROOT
from ..crossgpu.catalog import compile_catalog, REQUIRED
from ..crossgpu.features import initial_state
from ..nvidia.model import NvidiaModel
from ..run import save_json, sha256

SOURCE = ROOT/"results/offline-v4"
DEFAULT_BUNDLE = ROOT/"models/pod-gpu-rf-v1"
CLUSTER_MODELS = ("rtx4090","rtx5090","l40")


def freeze(destination=DEFAULT_BUNDLE):
    destination = Path(destination)
    if destination.exists() and any(destination.iterdir()):
        manifest = json.loads((destination/"manifest.json").read_text(encoding="utf-8"))
        for name,digest in manifest["artifact_sha256"].items():
            if sha256(destination/name) != digest:
                raise ValueError(f"Frozen bundle changed: {name}")
        return manifest
    devices,_ = compile_catalog(SOURCE/"source-records.json")
    candidates = {}
    for name in ("known_states","B_multi_architecture"):
        model = NvidiaModel.load(SOURCE/name/"selected-model.joblib")
        coverage = {}
        for key in CLUSTER_MODELS:
            d = devices[key]
            initial = model.predict_device(d,initial_state(d))
            ranges = model.metadata["static_core_training_ranges"]
            coverage[key] = {"in_fitted_device_ids":key in model.metadata["train_device_ids"],
                "inside_static_core_ranges":all(ranges[k][0] <= d.values()[k] <= ranges[k][1] for k in REQUIRED),
                "initial_label":initial["label"],"initial_reliability":initial["reliability"],
                "related_validation_models":[r["device_id"] for r in initial.get("related_validation_models",[])]}
        candidates[name] = coverage
    chosen = "known_states"
    if not all(v["in_fitted_device_ids"] and v["inside_static_core_ranges"] and v["initial_reliability"] == "provisional_synthetic_validation_only" for v in candidates[chosen].values()):
        raise ValueError("Default model does not cover all three actual fleet profiles")
    destination.mkdir(parents=True,exist_ok=True)
    copies = {"random_forest.joblib":SOURCE/chosen/"selected-model.joblib","source-records.json":SOURCE/"source-records.json",
              "model-metadata.json":SOURCE/chosen/"model-metadata.json","reference-contract.json":SOURCE/"reference-contract.json",
              "scoring-policy.json":ROOT/"data/synthetic/scoring-policy.json","dataset-summary.json":SOURCE/"dataset-summary.json"}
    for name,path in copies.items():
        shutil.copy2(path,destination/name)
    metadata = json.loads((destination/"model-metadata.json").read_text(encoding="utf-8"))
    manifest = {"bundle_schema":"pod-gpu-bundle/v1","model_version":"rf-v4-known-states-"+sha256(copies["random_forest.joblib"])[:12],
                "selected_protocol":chosen,"selection_basis":"fitted coverage of 4090/5090/L40, numerical ranges and related validation evidence; not highest test score",
                "candidate_coverage":candidates,"feature_order":metadata["feature_names"],"training_parameters":metadata["hyperparameters"],
                "dataset_version":{"round":4,"seed":metadata["seed"],"dataset_summary_sha256":sha256(copies["dataset-summary.json"]),
                                   "source_records_sha256":sha256(copies["source-records.json"])},
                "label_contract_id":"cross-gpu-fp32-synthetic-v3","label_source":"synthetic","real_performance_guarantee":False,
                "quota_mapping":"HAMi UsedCore/100 accounting can map to synthetic quota feature; linear performance scaling remains hypothetical",
                "running_count_semantics":"RF training/inference counts are typed vGPU instances. API also tracks distinct Pod counts separately.",
                "artifact_sha256":{name:sha256(destination/name) for name in copies}}
    save_json(destination/"manifest.json",manifest)
    return manifest
