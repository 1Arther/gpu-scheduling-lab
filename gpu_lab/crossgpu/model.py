"""Two-target RF prototype. Its vote fractions are NOT calibrated reliability."""
from collections import Counter
from pathlib import Path

import joblib
import numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.metrics import accuracy_score, classification_report, confusion_matrix, f1_score
from sklearn.pipeline import Pipeline

from .catalog import STATIC
from .features import FEATURES, features
from .theory import LABELS, NAMES, UNKNOWN, assess, combine, hard_checks, reasons_unusable


def matrix(rows):
    return np.asarray([[r.get(k) if r.get(k) is not None else np.nan for k in FEATURES] for r in rows], dtype=float)


def metrics(rows, predictions):
    truth, predicted = [r["label"] for r in rows], [p["label"] for p in predictions]
    report = classification_report(truth, predicted, labels=list(LABELS), output_dict=True, zero_division=0)
    return {"samples": len(rows), "truth_kind": "synthetic", "real_performance_labels": 0,
            "accuracy": float(accuracy_score(truth, predicted)),
            "macro_f1": float(f1_score(truth, predicted, labels=list(LABELS), average="macro", zero_division=0)),
            "misclassified": sum(a != b for a, b in zip(truth, predicted)),
            "labels": list(LABELS), "class_counts": dict(Counter(truth)),
            "per_class_precision_recall_f1_support": {k: report[k] for k in LABELS},
            "confusion_matrix_true_rows_predicted_columns": confusion_matrix(truth, predicted, labels=list(LABELS)).tolist(),
            "targets": {kind: {"accuracy": float(accuracy_score([r[kind + "_adapted"] for r in rows], [p[kind] for p in predictions])),
                               "precision_recall_f1": classification_report([r[kind + "_adapted"] for r in rows], [p[kind] for p in predictions], output_dict=True, zero_division=0)}
                        for kind in ("training", "inference")}}


class CrossGPUModel:
    def __init__(self, pipeline, metadata):
        self.pipeline, self.metadata = pipeline, metadata

    def save(self, path):
        joblib.dump(self, Path(path), compress=3)

    @classmethod
    def load(cls, path):
        obj = joblib.load(Path(path))
        if not isinstance(obj, cls):
            raise TypeError("Not a third-round model")
        return obj

    def raw_predict(self, feature_rows):
        if not feature_rows:
            return []
        x = matrix(feature_rows)
        predicted = self.pipeline.predict(x)
        probabilities = self.pipeline.predict_proba(x)
        classes = self.pipeline.named_steps["rf"].classes_
        votes = []
        for c, p in zip(classes, probabilities):
            match = np.flatnonzero(c == True)
            votes.append(p[:, int(match[0])] if len(match) else np.zeros(len(x)))
        return [{"training": bool(p[0]), "inference": bool(p[1]), "label": combine(bool(p[0]), bool(p[1])),
                 "training_vote_fraction": float(votes[0][i]), "inference_vote_fraction": float(votes[1][i]),
                 "vote_fraction_is_reliability": False} for i, p in enumerate(predicted)]

    def predict_device(self, device, state, *, raw_prediction=None):
        invalid = reasons_unusable(device, state)
        missing = [k for k, v in features(device, state).items() if v is None]
        result = {"device_id": device.device_id, "model": device.record["model"],
                  "label_source": "model_prediction_not_ground_truth", "underlying_supervision": "synthetic",
                  "known_model": device.device_id in self.metadata["train_device_ids"],
                  "missing_or_masked_features": missing, "vote_fraction_is_reliability": False,
                  "hard_constraints": hard_checks(device, state), "reasons": invalid.copy(),
                  "raw_rf": None, "coverage": {}, "empirical_performance_verified": False}
        if invalid:
            result.update(label=UNKNOWN, label_zh=NAMES[UNKNOWN], training=None, inference=None,
                          reliability="insufficient_information", fallback=assess(device, state, .8))
            return result
        x, issues, coverage = features(device, state), [], {}
        for key in STATIC:
            value, limits = x[key], self.metadata["numeric_coverage"].get(key)
            if value is None or limits is None:
                continue
            lo, hi = limits
            if value < lo or value > hi:
                coverage[key] = {"value": value, "training_min": lo, "training_max": hi,
                                 "extreme": value < lo * .5 or value > hi * 2}
        if coverage:
            issues.append("outside_training_numeric_range")
        if device.record["software_backend"] not in self.metadata["train_backends"]:
            issues.append("unvalidated_software_backend")
        if self.metadata["selected_validation_macro_f1"] < .75:
            issues.append("validation_quality_gate_failed")
        if not self.metadata["validation_has_all_four_classes"]:
            issues.append("validation_missing_reference_class")
        raw = raw_prediction if raw_prediction is not None else self.raw_predict([x])[0]
        result.update(raw_rf=raw, coverage=coverage,
                      fallback=assess(device, state, .8), reasons=issues,
                      reliability_basis="held-out SYNTHETIC rule agreement + numeric/backend coverage; not real benchmark validation")
        if issues:
            result.update(label=UNKNOWN, label_zh=NAMES[UNKNOWN], training=None, inference=None,
                          reliability="low_coverage_pending_verification")
        else:
            train = raw["training"] and result["hard_constraints"]["training"]
            infer = raw["inference"] and result["hard_constraints"]["inference"]
            label = combine(train, infer)
            result.update(training=train, inference=infer, label=label, label_zh=NAMES[label],
                          reliability="provisional_synthetic_model_only",
                          reasons=["RF numeric prediction within declared synthetic coverage; reference-specific, not a universal GPU property"])
        return result


def train(splits, devices, seed=20261016, trees=180):
    train_rows, validation = splits["train"], splits["validation"]
    if any(r["label_source"] != "synthetic" for r in train_rows + validation):
        raise ValueError("This synthetic-only prototype rejects unknown/self-predicted labels and requires a separate audited path for real labels")
    x = matrix([r["features"] for r in train_rows])
    y = np.asarray([[r["training_adapted"], r["inference_adapted"]] for r in train_rows], dtype=bool)
    candidates, selection = [], []
    # Fixed small validation-only choice. Test rows never enter fitting/selection.
    for leaf in (2, 5):
        pipe = Pipeline([("imputer", SimpleImputer(strategy="median", add_indicator=True, keep_empty_features=True)),
                         ("rf", RandomForestClassifier(n_estimators=trees, min_samples_leaf=leaf,
                                                       max_features=.85, random_state=seed, n_jobs=1))])
        pipe.fit(x, y)
        model = CrossGPUModel(pipe, {})
        score = metrics(validation, model.raw_predict([r["features"] for r in validation]))
        selection.append({"min_samples_leaf": leaf, "validation_accuracy": score["accuracy"], "validation_macro_f1": score["macro_f1"]})
        candidates.append(model)
    chosen = max(range(len(candidates)), key=lambda i: (selection[i]["validation_macro_f1"], selection[i]["validation_accuracy"]))
    model = candidates[chosen]
    ids = sorted({r["device_id"] for r in train_rows})
    coverage = {}
    for key in STATIC:
        values = [r["features"][key] for r in train_rows if r["features"][key] is not None]
        coverage[key] = [min(values), max(values)] if values else None
    model.metadata = {"model_type": "multi-output RandomForestClassifier", "seed": seed, "feature_names": list(FEATURES),
                      "identity_features_excluded": ["device_id", "model", "vendor", "architecture", "software_backend"],
                      "train_device_ids": ids, "train_model_groups": sorted({r["model_group"] for r in train_rows}),
                      "train_backends": sorted({devices[i].record["software_backend"] for i in ids}),
                      "numeric_coverage": coverage, "selection": selection, "chosen_candidate": chosen,
                      "selected_validation_macro_f1": selection[chosen]["validation_macro_f1"],
                      "validation_class_counts": dict(Counter(r["label"] for r in validation)),
                      "validation_has_all_four_classes": set(r["label"] for r in validation) == set(LABELS),
                      "hyperparameters": {"n_estimators": trees, "min_samples_leaf": selection[chosen]["min_samples_leaf"], "max_features": .85},
                      "fit_samples": len(train_rows), "imputer_fit_scope": "training rows ONLY; optional null -> train median + missing indicator; all-null -> 0 + indicator",
                      "supervision": "synthetic theoretical FP32 reference contracts ONLY", "test_used_for_selection": False,
                      "missing_required_behavior": "abstain before imputation", "coverage_policy": "any observed static range violation or unseen backend -> abstain; architecture name ignored"}
    return model
