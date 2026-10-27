"""Classifiers evaluated on unchanged v1 splits; rule agreement is not performance."""
from collections import Counter
import csv
import hashlib
import json
from pathlib import Path

from sklearn.metrics import accuracy_score, classification_report, confusion_matrix, f1_score

from .dataset import audit_splits
from .features import FEATURE_NAMES
from .labeling import LABELS, ReferenceOracle
from .model import SuitabilityModel, evaluate_model, train_model
from .profiles import DEFAULT_PROFILES
from .resources import Allocation, GPUState, ResourceRequest
from .run import save_json, sha256

STATIC_FEATURES = FEATURE_NAMES[:5]


def read_jsonl(path):
    with Path(path).open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def write_csv(path, rows, fieldnames=None):
    rows = list(rows)
    fields = fieldnames or list(dict.fromkeys(k for row in rows for k in row))
    with Path(path).open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def restore_state(row, profiles):
    snapshot = row["state"]
    state = GPUState(snapshot["gpu_id"], profiles[row["profile_id"]],
                     snapshot["allocated_instances"] + snapshot["remaining_instances"])
    for a in snapshot["allocations"]:
        state.allocate(Allocation(a["allocation_id"], a["task_type"], ResourceRequest(**a["request"])))
    return state


def prediction_metrics(rows, predicted):
    truth = [r["label"] for r in rows]
    report = classification_report(truth, predicted, labels=list(LABELS), output_dict=True, zero_division=0)
    return {"samples": len(rows), "accuracy": float(accuracy_score(truth, predicted)),
            "macro_f1": float(f1_score(truth, predicted, labels=list(LABELS), average="macro", zero_division=0)),
            "misclassified": sum(a != b for a, b in zip(truth, predicted)),
            "labels": list(LABELS), "per_class": {k: report[k] for k in LABELS},
            "confusion_matrix_true_rows_predicted_columns": confusion_matrix(truth, predicted, labels=list(LABELS)).tolist()}


def run_classification_comparison(first_round: Path, output: Path, profiles):
    splits = {n: read_jsonl(first_round / f"{n}.jsonl") for n in ("train", "validation", "test")}
    audit = audit_splits(splits)
    # Preserve and reuse the first round's fitted dynamic RF, do not refit it.
    dynamic = SuitabilityModel.load(first_round / "random_forest.joblib")
    if sha256(DEFAULT_PROFILES) != dynamic.metadata["hardware_catalog_sha256"]:
        raise ValueError("Hardware catalog changed since v1; do not silently relabel/reuse these splits")
    source_manifest = {f.name: sha256(f) for f in sorted(first_round.iterdir()) if f.is_file()}
    save_json(output / "first-round-preserved-sha256.json", source_manifest)
    candidates, selection = [], []
    # The v1 rare-class weights are unsuitable for constant-per-card features.
    # Select only on frozen VALIDATION rows, never test or scheduling outcomes.
    for class_weight in (None, "balanced_subsample"):
        candidate = train_model(splits["train"], seed=dynamic.metadata["seed"],
                                trees=dynamic.metadata["hyperparameters"]["n_estimators"], feature_names=STATIC_FEATURES,
                                class_weight=class_weight,
                                metadata={"feature_scope": "static_only", "source_splits": audit})
        validation, _ = evaluate_model(candidate, splits["validation"])
        candidates.append(candidate)
        selection.append({"class_weight": class_weight, "validation_accuracy": validation["accuracy"],
                          "validation_macro_f1": validation["macro_f1"]})
    chosen = max(range(len(candidates)), key=lambda i: (selection[i]["validation_macro_f1"], selection[i]["validation_accuracy"]))
    static = candidates[chosen]
    static.save(output / "static-random-forest.joblib")
    oracle = ReferenceOracle.load(first_round / "reference-contract.json")
    results, csv_rows = {}, []
    for name, rows in splits.items():
        manual = [oracle.evaluate(restore_state(r, profiles))["label"] for r in rows]
        if manual != [r["label"] for r in rows]:
            raise ValueError("Saved labels no longer match the frozen v1 reference contract")
        models = {"manual_rule": manual,
                  "static_rf": evaluate_model(static, rows)[1],
                  "dynamic_rf": evaluate_model(dynamic, rows)[1]}
        results[name] = {}
        for method, predicted in models.items():
            metrics = prediction_metrics(rows, predicted)
            results[name][method] = metrics
            csv_rows.append({"split": name, "method": method, **{k: metrics[k] for k in ("samples", "accuracy", "macro_f1", "misclassified")}})
            if name == "test":
                write_csv(output / f"test-errors-{method}.csv", [
                    {"sample_id": r["sample_id"], "profile_id": r["profile_id"], "reference_label": r["label"], "predicted_label": p,
                    **r["features"]} for r, p in zip(rows, predicted) if r["label"] != p
                ], fieldnames=["sample_id", "profile_id", "reference_label", "predicted_label", *FEATURE_NAMES])
    comparison = {"data_kind": "agreement_with_artificial_reference_labels_not_empirical_performance_labels",
                  "splits_preserved": audit, "results": results,
                  "manual_rule_interpretation": "100% is tautological: the evaluated rule generated these labels",
                  "static_feature_names": list(STATIC_FEATURES), "dynamic_feature_names": dynamic.metadata["feature_names"],
                  "static_validation_selection": {"candidates": selection, "selected": selection[chosen],
                                                  "criterion": "validation macro-F1, then validation accuracy"},
                  "real_gpu_performance_measurements": 0}
    save_json(output / "classification-comparison.json", comparison)
    write_csv(output / "classification-comparison.csv", csv_rows)
    return static, dynamic, comparison, source_manifest
