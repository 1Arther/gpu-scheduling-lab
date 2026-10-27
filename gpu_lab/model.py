"""RandomForest predicts from raw features, without consulting the label oracle."""
from dataclasses import dataclass
from pathlib import Path

import joblib
import numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.metrics import accuracy_score, classification_report, confusion_matrix, f1_score
from sklearn.pipeline import Pipeline

from .features import FEATURE_NAMES, feature_values, state_features

# Deliberately independent of labeling implementation at inference time.
CLASSES = ("training_preferred", "inference_preferred", "balanced", "unavailable")


def matrix(rows, feature_names=FEATURE_NAMES):
    return np.asarray([[r["features"][name] for name in feature_names] for r in rows], dtype=float)


@dataclass
class SuitabilityModel:
    pipeline: Pipeline
    metadata: dict

    def predict(self, state):
        return self.predict_many([state])[0]

    def predict_many(self, states):
        if not states:
            return []
        if any(s.profile.profile_id not in self.metadata["profile_ids"] for s in states):
            raise ValueError("Unseen hardware profile: evaluate and retrain before use")
        names = self.metadata.get("feature_names", list(FEATURE_NAMES))
        x = np.asarray([[state_features(s)[name] for name in names] for s in states], dtype=float)
        result = []
        for probabilities in self.pipeline.predict_proba(x):
            label = str(self.pipeline.classes_[np.argmax(probabilities)])
            by_class = {str(k): float(v) for k, v in zip(self.pipeline.classes_, probabilities)}
            result.append({"label": label, "class_probabilities": {k: by_class.get(k, 0.0) for k in CLASSES},
                           "prediction_kind": "RandomForest_prediction_of_synthetic_reference_class",
                           "note": "Vote probabilities are not calibrated performance guarantees"})
        return result

    def save(self, path):
        joblib.dump(self, Path(path), compress=3)

    @classmethod
    def load(cls, path):
        """Only load trusted artifacts produced by this project (joblib uses pickle)."""
        model = joblib.load(Path(path))
        if not isinstance(model, cls):
            raise ValueError("Not a GPU lab model")
        return model


def train_model(train_rows, seed=20261016, trees=240, metadata=None, feature_names=FEATURE_NAMES,
                class_weight="balanced_subsample"):
    labels = np.array([r["label"] for r in train_rows])
    if set(labels) != set(CLASSES):
        raise ValueError("Training data must contain all four reference classes")
    pipeline = Pipeline([
        # Fit optional-feature imputation ONLY on training rows. No scaling needed for RF.
        ("imputer", SimpleImputer(strategy="median", add_indicator=True, keep_empty_features=True)),
        ("forest", RandomForestClassifier(n_estimators=trees, max_features=0.8,
                                         min_samples_leaf=2, class_weight=class_weight,
                                         random_state=seed, n_jobs=1)),
    ])
    pipeline.fit(matrix(train_rows, feature_names), labels)
    return SuitabilityModel(pipeline, {
        "seed": seed,
        "algorithm": "RandomForestClassifier",
        "feature_names": list(feature_names),
        "profile_ids": sorted({r["profile_id"] for r in train_rows}),
        "fit_split": "train only",
        "hyperparameters": {"n_estimators": trees, "max_features": 0.8,
                            "min_samples_leaf": 2, "class_weight": class_weight, "n_jobs": 1},
        "test_use": "single final evaluation; no test-set parameter selection",
        "generalization": "same three public hardware profiles, new synthetic resource states",
        **(metadata or {}),
    })


def evaluate_model(model, rows):
    y = [r["label"] for r in rows]
    predicted = model.pipeline.predict(matrix(rows, model.metadata["feature_names"]))
    report = classification_report(y, predicted, labels=list(CLASSES), output_dict=True, zero_division=0)
    return {
        "samples": len(rows),
        "accuracy": float(accuracy_score(y, predicted)),
        "macro_f1": float(f1_score(y, predicted, labels=list(CLASSES), average="macro", zero_division=0)),
        "labels": list(CLASSES),
        "per_class": {k: report[k] for k in CLASSES},
        "confusion_matrix_true_rows_predicted_columns": confusion_matrix(y, predicted, labels=list(CLASSES)).tolist(),
    }, predicted.tolist()
