"""RF selection on validation only; local evidence guards independent of votes."""
from collections import Counter
import math
from pathlib import Path

import joblib
import numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.metrics import f1_score
from sklearn.pipeline import Pipeline

from ..crossgpu.catalog import REQUIRED
from ..crossgpu.model import metrics as base_metrics
from ..crossgpu.theory import LABELS, NAMES, UNKNOWN, assess, combine, hard_checks, reasons_unusable
from .features import FEATURE_SETS, enrich, features


def matrix(rows, names):
    enriched = [enrich(r) for r in rows]
    return np.asarray([[r.get(k) if r.get(k) is not None else np.nan for k in names] for r in enriched], dtype=float)


def metrics(rows, predictions):
    result = base_metrics(rows, predictions)
    observed = sorted({r["label"] for r in rows})
    result["observed_true_classes"] = observed
    result["macro_f1_observed_classes"] = float(f1_score([r["label"] for r in rows], [p["label"] for p in predictions], labels=observed, average="macro", zero_division=0))
    result["complete_four_class_truth_coverage"] = set(observed) == set(LABELS)
    return result


def probabilities(t, i):
    return {"training_preferred": t*(1-i), "inference_preferred": (1-t)*i,
            "balanced": t*i, "unavailable": (1-t)*(1-i)}


class NvidiaModel:
    def __init__(self, pipeline, metadata):
        self.pipeline, self.metadata = pipeline, metadata

    def save(self, path):
        joblib.dump(self, Path(path), compress=3)

    @classmethod
    def load(cls, path):
        model = joblib.load(Path(path))
        if not isinstance(model, cls):
            raise TypeError("Expected fourth-round NVIDIA model")
        return model

    def raw_predict(self, inputs):
        if not inputs:
            return []
        probs = self.pipeline.predict_proba(matrix(inputs, self.metadata["feature_names"]))
        votes = []
        for classes, p in zip(self.pipeline.named_steps["rf"].classes_, probs):
            ix = np.flatnonzero(classes == True)
            votes.append(p[:, int(ix[0])] if len(ix) else np.zeros(len(inputs)))
        result = []
        for t, i in zip(*votes):
            training, inference = bool(t > .5), bool(i > .5)
            result.append({"training": training, "inference": inference, "label": combine(training, inference),
                           "training_vote_fraction": float(t), "inference_vote_fraction": float(i),
                           "class_probabilities": probabilities(float(t), float(i)),
                           "class_probability_basis": "product of two RF marginal estimates; independence approximation, not a calibrated joint posterior",
                           "vote_fraction_is_reliability": False})
        return result

    def related_evidence(self, device):
        v, policy = device.values(comparable=True), self.metadata["quality_policy"]
        if any(v[k] is None or v[k] <= 0 for k in REQUIRED):
            return [], "none"
        nearby = []
        for row in self.metadata["validation_devices"]:
            distance = max(abs(math.log2(v[k] / row["hardware"][k])) for k in REQUIRED)
            if distance <= policy["max_validation_neighbor_log2_distance"] + 1e-12:
                nearby.append({**row, "log2_distance": distance})
        same = [r for r in nearby if device.record.get("architecture") is not None and r["architecture"] == device.record.get("architecture")]
        if same:
            return same, "related_architecture_validation"
        if len(nearby) >= policy["numeric_fallback_min_validation_models"]:
            return nearby, "numeric_capability_neighbor_validation"
        return [], "no_related_validation_evidence"

    def predict_device(self, device, state, *, raw_prediction=None):
        invalid = reasons_unusable(device, state)
        if device.record["vendor"] != "NVIDIA" or device.record["software_backend"] != "CUDA":
            invalid.append("outside_NVIDIA_CUDA_synthetic_scope")
        hard = hard_checks(device, state)
        result = {"device_id": device.device_id, "model": device.record["model"], "underlying_supervision": "synthetic",
                  "label_source": "model_prediction_not_ground_truth", "known_model": device.device_id in self.metadata["train_device_ids"],
                  "hard_constraints": hard, "raw_rf": None, "class_probabilities": None,
                  "vote_fraction_is_reliability": False, "empirical_performance_verified": False,
                  "missing_or_masked_features": [k for k,v in features(device, state).items() if v is None],
                  "fallback": assess(device, state, .8), "reasons": invalid.copy()}
        if invalid:
            result.update(training=None, inference=None, label=UNKNOWN, label_zh=NAMES[UNKNOWN], reliability="insufficient_information")
            return result
        raw = raw_prediction if raw_prediction is not None else self.raw_predict([features(device, state)])[0]
        result.update(raw_rf=raw, class_probabilities=raw["class_probabilities"])
        violations = {}
        for key in REQUIRED:
            lo, hi = self.metadata["static_core_training_ranges"][key]
            value = device.values(comparable=True)[key]
            if not lo <= value <= hi:
                violations[key] = {"value": value, "train_min": lo, "train_max": hi}
        result["outside_training_ranges"] = violations
        issues = ["outside_training_static_core_range"] if violations else []
        related, mode = self.related_evidence(device)
        result["evidence_mode"] = mode
        result["related_validation_models"] = [{k:r[k] for k in ("device_id","architecture","log2_distance")} for r in related]
        target_evidence, decision = {}, {}
        policy = self.metadata["quality_policy"]
        for target in ("training", "inference"):
            if not hard[target]:
                decision[target] = False
                target_evidence[target] = {"basis": "deterministic_reference_resource_filter", "supported": True}
                continue
            sign = str(int(raw[target]))
            cells = [r["cells"][target][sign] for r in related]
            support, correct = sum(c["support"] for c in cells), sum(c["correct"] for c in cells)
            episodes = sum(c["episodes"] for c in cells)
            precision = correct/support if support else None
            supported = support >= policy["min_prediction_sign_support"] and episodes >= policy["min_prediction_sign_episodes"] and precision >= policy["min_prediction_sign_precision"]
            target_evidence[target] = {"basis": mode, "predicted_sign": raw[target], "validation_sign_support": support,
                                       "validation_sign_correct": correct, "validation_sign_precision": precision,
                                       "validation_sign_episodes": episodes, "supported": supported,
                                       "not_a_calibrated_confidence_interval": True}
            decision[target] = raw[target]
            if not supported:
                issues.append(f"insufficient_related_validation_for_{target}_sign_{sign}")
        result["target_validation_evidence"] = target_evidence
        if issues:
            result.update(training=None, inference=None, label=UNKNOWN, label_zh=NAMES[UNKNOWN],
                          reliability="pending_related_validation_or_coverage", reasons=issues)
        else:
            label = combine(decision["training"], decision["inference"])
            result.update(**decision, label=label, label_zh=NAMES[label], reliability="provisional_synthetic_validation_only",
                          reasons=["resource constraints and related binary validation cells passed; no real performance assurance"])
        return result


def add_evidence(model, train_rows, val_rows, devices, policy):
    ids = sorted({r["device_id"] for r in train_rows})
    predictions = model.raw_predict([r["features"] for r in val_rows])
    evidence = []
    for device_id in sorted({r["device_id"] for r in val_rows}):
        pairs = [(r,p) for r,p in zip(val_rows,predictions) if r["device_id"] == device_id]
        cells = {}
        for target in ("training","inference"):
            cells[target] = {}
            for sign in (False,True):
                chosen = [(r,p) for r,p in pairs if p[target] == sign]
                cells[target][str(int(sign))] = {"support": len(chosen),
                    "correct": sum(p[target] == r[target+"_adapted"] for r,p in chosen),
                    "episodes": len({r["episode_id"] for r,p in chosen})}
        d = devices[device_id]
        evidence.append({"device_id": device_id, "architecture": d.record["architecture"],
                         "hardware": {k:d.values(comparable=True)[k] for k in REQUIRED}, "cells": cells})
    model.metadata.update(train_device_ids=ids, validation_devices=evidence, quality_policy=policy,
                          static_core_training_ranges={k:[min(devices[i].values()[k] for i in ids), max(devices[i].values()[k] for i in ids)] for k in REQUIRED},
                          validation_four_class_counts=dict(Counter(r["label"] for r in val_rows)),
                          no_global_missing_class_rejection=True)


def select_models(splits, devices, plan):
    """Deliberately never iterates splits['test']. Selection is frozen before testing."""
    tr, va, settings = splits["train"], splits["validation"], plan["training"]
    if any(r["label_source"] != "synthetic" for r in tr+va):
        raise ValueError("Only audited synthetic labels belong in this experiment")
    y = np.asarray([[r["training_adapted"], r["inference_adapted"]] for r in tr], dtype=bool)
    models, comparisons = {}, []
    for variant in plan["feature_sets"]:
        names, candidates = FEATURE_SETS[variant], []
        x = matrix([r["features"] for r in tr], names)
        for leaf in settings["min_samples_leaf"]:
            pipeline = Pipeline([("imputer", SimpleImputer(strategy="median", add_indicator=True, keep_empty_features=True)),
                                 ("rf", RandomForestClassifier(n_estimators=settings["trees"], min_samples_leaf=leaf,
                                                               max_features=settings["max_features"], random_state=settings["seed"], n_jobs=1))])
            pipeline.fit(x, y)
            model = NvidiaModel(pipeline, {"feature_names": list(names), "feature_set": variant, "seed": settings["seed"],
                                "hyperparameters": {"n_estimators": settings["trees"], "min_samples_leaf": leaf, "max_features":settings["max_features"]},
                                "identity_features_excluded": ["device_id","model","vendor","architecture"], "supervision":"synthetic_theoretical_contract_v3",
                                "test_used_for_selection": False, "imputer_fit_scope":"training rows only"})
            m = metrics(va, model.raw_predict([r["features"] for r in va]))
            candidates.append((model,m))
        chosen = max(candidates, key=lambda pair: (pair[1]["macro_f1_observed_classes"],pair[1]["accuracy"],-pair[0].metadata["hyperparameters"]["min_samples_leaf"]))
        model, validation = chosen
        add_evidence(model, tr, va, devices, plan["quality_policy"])
        model.metadata["validation_metrics"] = validation
        models[variant] = model
        comparisons.append({"feature_set":variant,"features":len(names),"validation_accuracy":validation["accuracy"],
                            "validation_macro_f1":validation["macro_f1"],"validation_observed_macro_f1":validation["macro_f1_observed_classes"],
                            "chosen_leaf":model.metadata["hyperparameters"]["min_samples_leaf"],
                            "leaf_candidates":[{"leaf":candidate.metadata["hyperparameters"]["min_samples_leaf"],"validation_accuracy":score["accuracy"],"validation_observed_macro_f1":score["macro_f1_observed_classes"]} for candidate,score in candidates]})
    selected = max(comparisons, key=lambda r:(r["validation_observed_macro_f1"],r["validation_accuracy"],-r["features"]))["feature_set"]
    return models, selected, comparisons
