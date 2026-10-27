from dataclasses import replace
import numpy as np
import pytest

from gpu_lab.dynamic import DynamicGPU
from gpu_lab.model import CLASSES, SuitabilityModel, evaluate_model, matrix, train_model
from gpu_lab.resources import Allocation, GPUState, ResourceRequest


def test_forest_reproducibility(splits):
    first = train_model(splits["train"], seed=7, trees=24)
    second = train_model(splits["train"], seed=7, trees=24)
    x = matrix(splits["test"])
    np.testing.assert_array_equal(first.pipeline.predict_proba(x), second.pipeline.predict_proba(x))


def test_evaluation_has_fixed_four_class_metrics(fitted, splits):
    for rows in splits.values():
        metrics, predictions = evaluate_model(fitted, rows)
        assert set(predictions) <= set(CLASSES)
        assert set(metrics["per_class"]) == set(CLASSES)
        assert len(metrics["confusion_matrix_true_rows_predicted_columns"]) == 4
        assert sum(sum(row) for row in metrics["confusion_matrix_true_rows_predicted_columns"]) == len(rows)
        assert 0 <= metrics["macro_f1"] <= 1


def test_saved_model_round_trip(fitted, profiles, tmp_path):
    target = tmp_path / "trusted.joblib"
    fitted.save(target)
    loaded = SuitabilityModel.load(target)
    state = GPUState("a", profiles["l40"])
    assert loaded.predict(state) == fitted.predict(state)


def test_missing_optional_specs_supported(fitted, profiles):
    p = replace(profiles["l40"], cuda_cores=None, tdp_w=None)
    prediction = fitted.predict(GPUState("test", p))
    assert prediction["label"] in CLASSES
    assert sum(prediction["class_probabilities"].values()) == pytest.approx(1)


def test_unseen_chip_not_claimed_supported(fitted, profiles):
    p = replace(profiles["l40"], profile_id="unseen-chip")
    with pytest.raises(ValueError, match="Unseen"):
        fitted.predict(GPUState("test", p))


def test_repredicts_every_commit_and_rejects_overallocation(profiles):
    class CountingPredictor:
        def __init__(self):
            self.revisions = []

        def predict(self, state):
            self.revisions.append(state.revision)
            return {"label": "balanced"}  # Even an optimistic prediction cannot bypass the ledger.

    predictor = CountingPredictor()
    state = GPUState("test", profiles["l40"])
    gpu = DynamicGPU(state, predictor)
    gpu.allocate(Allocation("a", "training", ResourceRequest(4096, 60)))
    with pytest.raises(ValueError):
        gpu.allocate(Allocation("b", "inference", ResourceRequest(4096, 50)))
    gpu.release("a")
    assert predictor.revisions == [0, 1, 2]
    assert len(gpu.history) == 3
    assert state.remaining_compute_units == 100


def test_rf_dynamic_recovery(fitted, profiles):
    gpu = DynamicGPU(GPUState("test", profiles["l40"]), fitted)
    original = gpu.history[0]["prediction"]
    gpu.allocate(Allocation("a", "training", ResourceRequest(4096, 30)))
    gpu.release("a")
    assert gpu.history[-1]["prediction"] == original


def test_prediction_does_not_call_reference_label_oracle(fitted, profiles, monkeypatch):
    from gpu_lab.labeling import ReferenceOracle

    def forbidden(*args):
        raise AssertionError("Prediction must not evaluate its own reference label")

    monkeypatch.setattr(ReferenceOracle, "evaluate", forbidden)
    assert fitted.predict(GPUState("test", profiles["l40"]))["label"] in CLASSES
