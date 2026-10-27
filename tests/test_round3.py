from copy import deepcopy
import math

import numpy as np
import pytest

from gpu_lab.crossgpu.adapter import LegacyAdapter
from gpu_lab.crossgpu.catalog import STATIC, Device, compile_catalog
from gpu_lab.crossgpu.data import generate, split_known, split_unseen, audit
from gpu_lab.crossgpu.demos import dynamic_demo, regression
from gpu_lab.crossgpu.features import FEATURES, features, from_ledger, initial_state, validate_state
from gpu_lab.crossgpu.model import CrossGPUModel, train
from gpu_lab.crossgpu.theory import UNKNOWN, assess, combine
from gpu_lab.resources import Allocation, GPUState, ResourceRequest
from gpu_lab.scoring import PredictionCache, PodScorer, PodRequest


@pytest.fixture(scope="module")
def catalog():
    return compile_catalog()[0]


@pytest.fixture(scope="module")
def cross_rows(catalog):
    return generate(catalog)[0]


@pytest.fixture(scope="module")
def cross_model(catalog, cross_rows):
    return train(split_known(cross_rows), catalog, trees=40)


def test_catalog_sources_dense_scope_units_and_nulls(catalog):
    assert len(catalog) == 22 and sum(d.eligible() for d in catalog.values()) == 16
    assert len({d.record["vendor"] for d in catalog.values()}) == 7
    for d in catalog.values():
        assert d.record["performance_label_source"] == "unknown"
        assert tuple(d.values()) == STATIC
        for s in d.record["specifications"].values():
            assert s["source_url"].startswith("https://") and s["locator"] and s["scope"]
    assert catalog["mi300x"].record["specifications"]["fp16_dense_tflops"]["raw_value"] == 1.3
    assert catalog["mi300x"].record["specifications"]["fp16_dense_tflops"]["raw_unit"] == "PFLOPS"
    assert catalog["mi250x"].values()["max_board_power_w"] == 560
    assert catalog["mi100"].values()["fp32_tflops"] == 23.1
    assert catalog["l4"].values()["fp16_dense_tflops"] is None
    assert catalog["mi300x"].values()["fp16_dense_tflops"] == 1300
    assert catalog["mi300x"].values(comparable=True)["fp16_dense_tflops"] is None
    assert all(v is None for v in catalog["ascend910"].values().values())


def test_only_numeric_features_no_identity_or_architecture(catalog):
    d = catalog["l40"]
    x = features(d, initial_state(d))
    assert tuple(x) == FEATURES and len(x) == 14
    assert all(v is None or isinstance(v, (float, int)) for v in x.values())
    clone = deepcopy(d.record)
    clone.update(device_id="a-future-id", model="a-future-name", architecture="unknown", vendor="unknown")
    assert features(Device(clone), initial_state(d)) == x


def test_known_split_has_unseen_states_and_no_duplicates(cross_rows):
    splits = split_known(cross_rows)
    result = audit(splits, model_disjoint=False)
    assert all(v == 0 for v in result["overlaps"].values())
    assert result["model_groups"]["train"] == result["model_groups"]["test"]


def test_unseen_split_never_shares_model_family(cross_rows):
    result = audit(split_unseen(cross_rows), model_disjoint=True)
    assert all(v == 0 for v in result["overlaps"].values())
    assert result["model_groups"]["test"] == ["a10", "mi300x", "mtt_s4000"]


def test_split_audit_rejects_copied_sample_or_self_label(cross_rows):
    splits = deepcopy(split_unseen(cross_rows))
    splits["test"].append(splits["train"][0])
    with pytest.raises(ValueError, match="leakage"):
        audit(splits, True)
    splits = deepcopy(split_known(cross_rows))
    splits["train"][0]["label_source"] = "model_prediction"
    with pytest.raises(ValueError, match="Unknown/self-predicted"):
        audit(splits, False)


def test_data_generator_repeatable_and_supervision_not_RF(catalog):
    a, ma = generate(catalog, seed=7, episodes=3, steps=5)
    b, mb = generate(catalog, seed=7, episodes=3, steps=5)
    assert a == b and ma == mb
    assert a != generate(catalog, seed=8, episodes=3, steps=5)[0]
    assert all(r["label_source"] == "synthetic" for r in a)
    assert ma["label_sources"]["measured"] == ma["label_sources"]["public_benchmark"] == 0
    assert not {r["device_id"] for r in a} & {"ascend910", "mlu370_x8", "biren166c", "intel_max1550"}


def test_reference_explains_compute_rich_memory_poor_and_reverse(catalog):
    d = catalog["l40"]
    s = from_ledger(GPUState("x", d))
    s.update(free_memory_mib=44000, free_compute_quota=40)
    p = assess(d, s)
    assert p["label"] == "training_preferred"
    assert p["details"]["training"]["theoretical_latency_s"] == pytest.approx(2/(90.5*.35*.4))
    s.update(free_memory_mib=7000, free_compute_quota=95)
    assert assess(d, s)["label"] == "inference_preferred"
    assert combine(None, True) == UNKNOWN


def test_dynamic_ledger_restores_resources_and_static_hardware(catalog, cross_model):
    events = dynamic_demo(cross_model, catalog["l40"])
    assert len(events) == 5
    assert len({e["hardware_sha256"] for e in events}) == 1
    assert events[0]["state"] == events[-1]["state"]
    assert [e["state"]["free_compute_quota"] for e in events] == [100, 40, 35, 95, 100]
    assert [e["state"]["free_memory_mib"] for e in events][2:4] == [6000, 7000]
    assert [e["independent_reference_rule"]["label"] for e in events] == ["balanced", "training_preferred", "unavailable", "inference_preferred", "balanced"]


@pytest.mark.parametrize("field,value", [("free_memory_mib", -1), ("free_compute_quota", 101),
                                        ("allocated_vgpu_count", 11), ("running_training_count", .5),
                                        ("free_compute_quota", float("nan")), ("free_memory_mib", "missing"),
                                        ("free_compute_quota", None), ("instance_limit", 0)])
def test_invalid_dynamic_inputs_abstain(catalog, cross_model, field, value):
    d = catalog["l40"]
    s = initial_state(d)
    s[field] = value
    assert validate_state(d, s)
    assert cross_model.predict_device(d, s)["label"] == UNKNOWN


def test_missing_critical_feature_never_imputed_into_certainty(catalog, cross_model, monkeypatch):
    d = deepcopy(catalog["l40"].record)
    d["specifications"]["fp32_tflops"]["value"] = None
    def forbidden(*args):
        raise AssertionError("RF must not see missing required capability")
    monkeypatch.setattr(cross_model, "raw_predict", forbidden)
    p = cross_model.predict_device(Device(d), initial_state(Device(d)))
    assert p["label"] == UNKNOWN and p["training"] is None and p["raw_rf"] is None


@pytest.mark.parametrize("value", [0, -1, float("inf")])
def test_invalid_critical_hardware_returns_pending(catalog, cross_model, value):
    rec = deepcopy(catalog["l40"].record)
    rec["specifications"]["fp32_tflops"]["value"] = value
    device = Device(rec)
    p = cross_model.predict_device(device, initial_state(device))
    assert p["label"] == UNKNOWN and "invalid_required:fp32_tflops" in p["reasons"]


def test_optional_missing_imputer_is_fitted_on_train_only(cross_model, cross_rows):
    imputer = cross_model.pipeline.named_steps["imputer"]
    train_rows = split_known(cross_rows)["train"]
    for index, name in enumerate(FEATURES):
        values = [r["features"][name] for r in train_rows if r["features"][name] is not None]
        expected = np.median(values) if values else 0
        assert imputer.statistics_[index] == pytest.approx(expected)
    assert cross_model.metadata["test_used_for_selection"] is False


def test_numeric_out_of_domain_and_unmapped_quota_return_fallback(catalog, cross_model):
    rec = deepcopy(catalog["l40"].record)
    rec["specifications"]["fp32_tflops"]["value"] = 99999
    d = Device(rec)
    p = cross_model.predict_device(d, initial_state(d))
    assert p["label"] == UNKNOWN and p["raw_rf"] is not None
    assert p["coverage"]["fp32_tflops"]["extreme"]
    assert p["fallback"]["conservative_factor"] == .8 and not p["vote_fraction_is_reliability"]
    p = cross_model.predict_device(catalog["l40"], initial_state(catalog["l40"], "vendor_native_cores"))
    assert p["label"] == UNKNOWN and "unmapped_backend_quota_semantics" in p["reasons"]


def test_identity_or_unknown_architecture_does_not_change_numeric_prediction(catalog, cross_model):
    d = catalog["l40"]
    rec = deepcopy(d.record)
    rec.update(device_id="unseen-device", architecture="unseen-architecture")
    unknown = Device(rec)
    before = cross_model.predict_device(d, initial_state(d))
    after = cross_model.predict_device(unknown, initial_state(unknown))
    assert before["raw_rf"] == after["raw_rf"] and before["label"] == after["label"]
    assert before["known_model"] and not after["known_model"]


def test_unknown_backend_and_NPU_abstain(catalog, cross_model):
    d = deepcopy(catalog["l40"].record)
    d["software_backend"] = "unvalidated_runtime"
    p = cross_model.predict_device(Device(d), initial_state(Device(d)))
    assert p["label"] == UNKNOWN and "unvalidated_software_backend" in p["reasons"]
    assert cross_model.predict_device(catalog["mlu370_x8"], initial_state(catalog["mlu370_x8"]))["label"] == UNKNOWN


def test_round3_model_serialization(tmp_path, catalog, cross_model):
    path = tmp_path / "model.joblib"
    cross_model.save(path)
    loaded = CrossGPUModel.load(path)
    d = catalog["l40"]
    assert cross_model.predict_device(d, initial_state(d)) == loaded.predict_device(d, initial_state(d))


def test_RF_seed_repeatable_and_test_never_used_for_fitting(catalog, cross_rows):
    splits = split_known(cross_rows)
    a = train(splits, catalog, seed=123, trees=20)
    # A poison test partition proves train()/validation selection do not read it.
    class ForbiddenTestPartition:
        def __iter__(self):
            raise AssertionError("Test partition used during fitting or model selection")
    copied = {**splits, "test": ForbiddenTestPartition()}
    b = train(copied, catalog, seed=123, trees=20)
    x = [r["features"] for r in splits["test"]]
    assert a.raw_predict(x) == b.raw_predict(x)
    assert a.metadata == b.metadata


def test_adapter_cache_repredicts_after_allocation_and_release(profiles, catalog, cross_model):
    ledger = GPUState("s", profiles["l40"])
    dynamic = PredictionCache(LegacyAdapter(cross_model, catalog))
    static = PredictionCache(LegacyAdapter(cross_model, catalog, static=True))
    before, static_before = dynamic.key(ledger), static.predict_many([ledger])[0]
    p = dynamic.predict_many([ledger])[0]
    ledger.allocate(Allocation("x", "training", ResourceRequest(1000, 60)))
    assert dynamic.key(ledger) != before
    assert static.predict_many([ledger])[0] == static_before
    q = dynamic.predict_many([ledger])[0]
    assert q["raw_rf"] != p["raw_rf"]
    ledger.release("x")
    assert dynamic.key(ledger) == before and dynamic.predict_many([ledger])[0] == p


def test_adapter_retains_hard_admission_checks(profiles, catalog, cross_model):
    scorer = PodScorer(LegacyAdapter(cross_model, catalog), profiles)
    ledger = GPUState("s", profiles["l40"])
    assert scorer.score(ledger, PodRequest("training", ResourceRequest(999999, 20)))["feasible"] is False
    assert not scorer.predictions.cache
    ledger.allocate(Allocation("all-but-small", "training", ResourceRequest(ledger.profile.modeled_memory_mib-1000, 95)))
    p = scorer.score(ledger, PodRequest("inference", ResourceRequest(512, 1)))
    assert p["feasible"] and p["prediction"]["label"] == "unavailable"


def test_three_strategy_compatibility_same_trace_no_overallocation_release(catalog, cross_model):
    result = regression(cross_model, catalog, seed=13, count=15)
    assert result["fleet"] == {"rtx4090": 4, "rtx5090": 4, "l40": 3}
    assert len({r["trace_sha256"] for r in result["summary"]}) == 1
    for name, audit_result in result["audit"].items():
        assert audit_result["completed"] == 15
        assert audit_result["no_overallocation"] and audit_result["all_resources_restored"]
        assert audit_result["resource_state_changes"] == 30
        if name == "dynamic_rf":
            assert audit_result["dynamic_repredictions"] == 30
