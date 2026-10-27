import pytest

from gpu_lab.features import state_features
from gpu_lab.resources import Allocation, GPUState, ResourceRequest


def test_four_reference_outcomes(profiles, oracle):
    state = GPUState("demo", profiles["l40"])
    assert oracle.evaluate(state)["label"] == "balanced"
    state.allocate(Allocation("compute", "training", ResourceRequest(1000, 60)))
    assert oracle.evaluate(state)["label"] == "training_preferred"
    state.allocate(Allocation("memory", "inference", ResourceRequest(state.profile.modeled_memory_mib - 7000, 5)))
    assert oracle.evaluate(state)["label"] == "unavailable"
    state.release("compute")
    assert oracle.evaluate(state)["label"] == "inference_preferred"
    state.release("memory")
    assert oracle.evaluate(state)["label"] == "balanced"


def test_labels_not_generated_by_memory_ratio_alone(profiles, oracle):
    labels = []
    for used_compute in (5, 60, 90):
        state = GPUState("same-memory", profiles["l40"])
        state.allocate(Allocation("a", "training", ResourceRequest(1000, used_compute)))
        assert state.remaining_memory_mib == profiles["l40"].modeled_memory_mib - 1000
        labels.append(oracle.evaluate(state)["label"])
    assert labels == ["balanced", "training_preferred", "unavailable"]


def test_no_instance_slots_blocks_references(profiles, oracle):
    state = GPUState("slots", profiles["rtx5090"])
    state.allocate(Allocation("a", "inference", ResourceRequest(10, 10, instances=10)))
    assert state.remaining_compute_units == 90
    assert oracle.evaluate(state)["label"] == "unavailable"


def test_reference_min_quota_is_not_automatically_a_performance_guarantee(profiles, oracle):
    assessment = oracle.evaluate(GPUState("test", profiles["l40"]))["reference_details"]
    assert assessment["training"]["required_compute_units_for_reference_slo"] == 32
    assert assessment["inference"]["required_compute_units_for_reference_slo"] == 53


def test_original_hardware_and_task_type_features_retained(profiles):
    state = GPUState("test", profiles["rtx4090"])
    before = state_features(state)
    state.allocate(Allocation("a", "training", ResourceRequest(4096, 20, instances=2)))
    state.allocate(Allocation("b", "inference", ResourceRequest(2048, 10)))
    after = state_features(state)
    for name in ("memory_gb", "fp32_tflops", "bandwidth_gb_s", "cuda_cores", "tdp_w"):
        assert before[name] == after[name]
    assert after["training_instances"] == 2
    assert after["inference_instances"] == 1
    assert after["allocated_instances"] == 3
