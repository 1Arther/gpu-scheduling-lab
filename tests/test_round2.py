import ast
from dataclasses import replace
import inspect
import math

import pytest

from gpu_lab import execution as execution_module
from gpu_lab.comparison import STATIC_FEATURES, restore_state
from gpu_lab.execution import ExecutionModel
from gpu_lab.model import matrix, train_model
from gpu_lab.profiles import ROOT
from gpu_lab.resources import Allocation, GPUState, ResourceRequest
from gpu_lab.scoring import PodRequest, PodScorer
from gpu_lab.simulation import simulate
from gpu_lab.strategies import Scheduler, STRATEGIES
from gpu_lab.workloads import generate_trace, Pod, SCENARIOS, trace_hash


@pytest.fixture(scope="module")
def static_model(splits):
    return train_model(splits["train"], seed=41, trees=40, feature_names=STATIC_FEATURES)


def test_static_model_is_invariant_to_dynamic_state(static_model, profiles):
    state = GPUState("same", profiles["l40"])
    before = static_model.predict(state)
    state.allocate(Allocation("a", "inference", ResourceRequest(40000, 95)))
    assert static_model.predict(state) == before


def test_static_model_has_only_hardware_inputs(static_model, splits):
    assert set(static_model.metadata["feature_names"]) == set(STATIC_FEATURES)
    assert matrix(splits["train"], STATIC_FEATURES).shape[1] == 5


def test_hard_constraints_checked_before_any_RF_call(profiles):
    class Forbidden:
        metadata = {"feature_names": []}

        def predict_many(self, states):
            raise AssertionError("Must not call RF on infeasible states")

    scorer = PodScorer(Forbidden(), profiles)
    state = GPUState("test", profiles["l40"])
    row = scorer.score(state, PodRequest("training", ResourceRequest(999999, 1)))
    assert row["feasible"] is False and row["score"] is None


def test_reference_unavailable_is_not_an_admission_gate(profiles):
    class Unavailable:
        metadata = {"feature_names": []}

        def predict_many(self, states):
            return [{"label": "unavailable", "class_probabilities": {"training_preferred": 0.,
                     "inference_preferred": 0., "balanced": 0., "unavailable": 1.}} for _ in states]

    scorer = PodScorer(Unavailable(), profiles)
    row = scorer.score(GPUState("test", profiles["l40"]), PodRequest("inference", ResourceRequest(512, 5)))
    assert row["feasible"] and row["score"] > 0


def test_scores_include_current_pod_and_resource_state(fitted, profiles):
    scorer = PodScorer(fitted, profiles)
    state = GPUState("test", profiles["rtx4090"])
    small = scorer.score(state, PodRequest("training", ResourceRequest(2048, 10)))
    large = scorer.score(state, PodRequest("inference", ResourceRequest(12000, 30)))
    assert small["score"] != large["score"]
    assert small["inputs"]["pod_type"] == "training"
    assert large["inputs"]["pod_memory_mib"] == 12000
    assert "training_instances" in small["inputs"]


def test_compact_guard_preserves_large_memory_card_and_does_not_block(profiles, fitted):
    path = ROOT / "data/synthetic/scoring-policy-memory-guard.json"
    scorer = PodScorer(fitted, profiles, path)
    small = GPUState("small", profiles["rtx4090"])
    large = GPUState("large", profiles["l40"])
    scores = scorer.score_many([small, large], PodRequest("training", ResourceRequest(20480, 20)))
    chosen = max(scores, key=lambda r: r["score"])
    assert chosen["gpu_id"] == "small"
    assert chosen["near_best_fit"]
    only_large = scorer.score_many([small, large], PodRequest("training", ResourceRequest(36864, 20)))
    assert not only_large[0]["feasible"] and only_large[1]["feasible"]
    assert only_large[1]["near_best_fit"]


def test_memory_guard_does_not_override_inference_ranking(profiles, fitted):
    scorer = PodScorer(fitted, profiles, ROOT / "data/synthetic/scoring-policy-memory-guard.json")
    row = scorer.score(GPUState("a", profiles["l40"]), PodRequest("inference", ResourceRequest(2048, 10)))
    assert "near_best_fit" not in row


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_trace_reproducibility_and_heterogeneity(scenario):
    a, b = generate_trace(scenario, 3, 30), generate_trace(scenario, 3, 30)
    assert a == b and trace_hash(a) == trace_hash(b)
    assert trace_hash(a) != trace_hash(generate_trace(scenario, 4, 30))
    assert len({p.request for p in a}) > 3


def test_duration_module_cannot_import_classifier_or_label_or_score():
    tree = ast.parse(inspect.getsource(execution_module))
    imports = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert not {"labeling", "model", "scoring", "run", "comparison"} & imports


def test_scheduler_view_excludes_hidden_execution_fields():
    pod = Pod("a", 0., "training", ResourceRequest(1024, 20), 1.)
    visible = PodRequest.from_pod(pod)
    assert not hasattr(visible, "reference_work_s")
    assert not hasattr(visible, "pressure")


def test_execution_varies_by_card_quota_and_colocation(profiles):
    model = ExecutionModel(seed=8)
    p = Pod("a", 0., "inference", ResourceRequest(1024, 25), 1.)
    s = GPUState("a", profiles["rtx4090"])
    base = model.rates(s, [p])["a"]
    fast = model.rates(GPUState("b", profiles["rtx5090"]), [p])["a"]
    smaller = replace(p, request=ResourceRequest(1024, 10))
    co = Pod("b", 0., "training", ResourceRequest(1024, 25), 1.)
    assert fast > base
    assert model.rates(s, [smaller])["a"] < base
    assert model.rates(s, [p, co])["a"] < base
    assert ExecutionModel(seed=8, interference=2).rates(s, [p, co])["a"] < model.rates(s, [p, co])["a"]
    assert model.rates(s, [p]) == ExecutionModel(seed=8).rates(s, [p])


def test_event_engine_recalculates_after_release(profiles):
    s = GPUState("one", profiles["l40"])
    pods = [Pod("a", 0., "training", ResourceRequest(1024, 50), 1.),
            Pod("b", 0., "training", ResourceRequest(1024, 50), 3.)]
    execution = ExecutionModel(seed=12)
    r = execution.rates(s, pods)
    first_finish = 1 / r["a"]
    solo_b = execution.rates(s, [pods[1]])["b"]
    second_finish = first_finish + (3 - r["b"] * first_finish) / solo_b
    result = simulate(pods, profiles, Scheduler("binpack", profiles), execution, 100, {"one": s})
    assert result["pods"][0]["finish_s"] == pytest.approx(first_finish)
    assert result["pods"][1]["finish_s"] == pytest.approx(second_finish)
    assert not s.allocations


def test_single_job_accounting_and_waiting(profiles):
    s = GPUState("one", profiles["l40"])
    pod = Pod("a", 0., "training", ResourceRequest(1024, 50), 1.)
    execution = ExecutionModel(seed=12)
    duration = 1 / execution.rates(s, [pod])["a"]
    result = simulate([pod], profiles, Scheduler("binpack", profiles), execution, duration * 10, {"one": s})
    assert result["metrics"]["mean_wait_s"] == 0
    assert result["metrics"]["reserved_compute_utilization"] == pytest.approx(0.05)
    assert result["metrics"]["reserved_memory_utilization"] == pytest.approx(0.1 * 1024 / s.profile.modeled_memory_mib)


@pytest.mark.parametrize("strategy", STRATEGIES)
def test_all_strategy_resource_invariants_and_completion(profiles, fitted, static_model, strategy):
    pods = generate_trace("bursty_mixed", seed=23, count=25)
    scheduler = Scheduler(strategy, profiles, static_model, fitted)
    result = simulate(pods, profiles, scheduler, ExecutionModel(seed=12), horizon_s=600)
    m = result["metrics"]
    assert m["completed_total_after_drain"] == len(pods)
    assert m["trace_sha256"] == trace_hash(pods)
    assert m["rejected_pods"] == 0
    assert 0 <= m["completed_within_horizon"] <= len(pods)
    assert 0 <= m["reserved_compute_utilization"] <= 1
    assert 0 <= m["reserved_memory_utilization"] <= 1
    for event in result["events"]:
        if "state_after" not in event:
            continue
        s = event["state_after"]
        assert 0 <= s["remaining_compute_units"] <= 100
        assert 0 <= s["remaining_memory_mib"] <= s["capacity_memory_mib"]
        assert 0 <= s["remaining_instances"] <= 10
        if strategy == "dynamic_rf":
            assert event["state_prediction_after"]["label"] in {"training_preferred", "inference_preferred", "balanced", "unavailable"}


def test_simulation_reproducible(profiles):
    pods = generate_trace("training_heavy", 7, 15)
    a = simulate(pods, profiles, Scheduler("binpack", profiles), ExecutionModel(seed=5))
    b = simulate(pods, profiles, Scheduler("binpack", profiles), ExecutionModel(seed=5))
    assert a == b


def test_unschedulable_request_is_rejected_not_deadlocked(profiles):
    pod = Pod("too-large", 0., "training", ResourceRequest(1000000, 10), 1.)
    result = simulate([pod], profiles, Scheduler("binpack", profiles), ExecutionModel())
    assert result["metrics"]["rejected_pods"] == 1
    assert result["metrics"]["completed_total_after_drain"] == 0


def test_observation_completion_count_and_drained_waits_are_separate(profiles):
    pod = Pod("long", 0., "training", ResourceRequest(1024, 10), 1000.)
    result = simulate([pod], profiles, Scheduler("binpack", profiles), ExecutionModel(), horizon_s=1)
    assert result["metrics"]["completed_within_horizon"] == 0
    assert result["metrics"]["completed_total_after_drain"] == 1
    assert result["metrics"]["unfinished_at_horizon"] == 1


def test_duplicate_id_fails(profiles):
    pod = Pod("duplicate", 0., "training", ResourceRequest(1024, 10), 1.)
    with pytest.raises(ValueError, match="Duplicate"):
        simulate([pod, pod], profiles, Scheduler("binpack", profiles), ExecutionModel())


def test_float_tolerance_cannot_start_a_pod_before_arrival(profiles):
    pod = Pod("tiny-arrival", 1e-10, "training", ResourceRequest(1024, 20), 1.)
    result = simulate([pod], profiles, Scheduler("binpack", profiles), ExecutionModel())
    assert result["pods"][0]["start_s"] >= pod.arrival_s
    assert result["pods"][0]["wait_s"] >= 0
