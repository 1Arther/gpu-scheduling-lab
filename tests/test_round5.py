from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import shutil
import subprocess
import sys
import threading
import time

import pytest

from gpu_lab.runtime import classify_gpu, rank_gpus
from gpu_lab.runtime.adapters import MockVolcanoAdapter, RuntimeScheduler, dispatch
from gpu_lab.runtime.bundle import DEFAULT_BUNDLE, freeze
from gpu_lab.runtime.pods import PodValidationError, parse_pod
from gpu_lab.runtime.pool import GPUPool, ReservationConflict
from gpu_lab.runtime.schema import make_fleet, digest
from gpu_lab.runtime.service import SuitabilityService
from gpu_lab.profiles import ROOT, load_profiles
from gpu_lab.fleet import create_fleet
from gpu_lab.workloads import generate_trace
from gpu_lab.execution import ExecutionModel
from gpu_lab.simulation import simulate


@pytest.fixture(scope="module")
def runtime():
    freeze()
    return SuitabilityService()


@pytest.fixture
def gpus(runtime):
    return make_fleet(runtime.catalog)


def pod(name="test",kind="training",memory=4096,cores=20,count=1):
    labels = {} if kind is None else {"gpu-lab/workload-type":kind}
    return {"apiVersion":"v1","kind":"Pod","metadata":{"name":name,"uid":name,"labels":labels},
            "spec":{"containers":[{"name":"workload","resources":{"limits":{"volcano.sh/vgpu-number":count,
                       "volcano.sh/vgpu-memory":memory,"volcano.sh/vgpu-cores":cores}}}]}}


@pytest.mark.parametrize("kind",["training","inference"])
def test_runtime_explicit_labels_and_resources(kind):
    parsed = parse_pod(pod(kind=kind))
    assert parsed.task_type == kind
    assert parsed.requests[0].memory_mib == 4096 and parsed.requests[0].compute_quota == 20


def test_missing_label_is_resource_fallback_and_invalid_is_error(runtime,gpus):
    result = runtime.rank_gpus(pod(kind=None),gpus)
    assert result["task_type"] is None and result["strategy"] == "ordinary_binpack"
    assert result["recommendation"] and all(r["rf_probability"] is None for r in result["eligible_gpus"])
    with pytest.raises(PodValidationError,match="Illegal"):
        parse_pod(pod(kind="Training"))
    assert runtime.rank_gpus(pod(kind="train"),gpus)["status"] == "invalid_input"
    assert runtime.rank_gpus("invalid: [",gpus)["status"] == "invalid_input"


def test_native_memory_only_percentage_defaults_and_requests_fallback(gpus):
    p = pod()
    limits = p["spec"]["containers"][0]["resources"]["limits"]
    limits.pop("volcano.sh/vgpu-number")
    assert parse_pod(p).requests[0].count == 1
    limits.pop("volcano.sh/vgpu-memory")
    limits["volcano.sh/vgpu-number"] = 1
    limits.pop("volcano.sh/vgpu-cores")
    parsed = parse_pod(p).requests[0]
    assert parsed.memory_percent == 100 and parsed.compute_quota == 0
    assert parsed.memory_for(gpus[0]) == gpus[0]["hardware"]["memory_capacity_mib"]
    p["spec"]["containers"][0]["resources"]["requests"] = {"volcano.sh/vgpu-memory":2048,"volcano.sh/vgpu-cores":30}
    parsed = parse_pod(p).requests[0]
    assert parsed.memory_mib == 2048 and parsed.compute_quota == 30


@pytest.mark.parametrize("key,value",[("volcano.sh/vgpu-number",0),("volcano.sh/vgpu-cores",101),("volcano.sh/vgpu-memory","8Gi"),("volcano.sh/vgpu-cores","500m")])
def test_bad_resource_units_are_not_silently_reinterpreted(key,value):
    p = pod()
    p["spec"]["containers"][0]["resources"]["limits"][key] = value
    with pytest.raises(PodValidationError):
        parse_pod(p)


def test_requests_limits_mismatch_and_requests_only_rejected():
    p = pod()
    resources = p["spec"]["containers"][0]["resources"]
    resources["requests"] = {"volcano.sh/vgpu-memory":1000}
    with pytest.raises(PodValidationError,match="disagree"):
        parse_pod(p)
    resources["requests"],resources["limits"] = resources["limits"],{}
    with pytest.raises(PodValidationError,match="requires"):
        parse_pod(p)


@pytest.mark.parametrize("change,reason",[
    ({"dynamic":{"free_memory_mib":100}},"insufficient_memory_mib"),
    ({"dynamic":{"free_compute_quota":1}},"insufficient_compute_quota"),
    ({"backend":"ROCm"},"software_backend_incompatible"),
    ({"healthy":False},"gpu_not_healthy"),
    ({"managed":False},"gpu_not_managed_by_supported_plugin"),
    ({"sharing_mode":"mig"},"unsupported_allocation_semantics"),
    ({"dynamic":{"allocated_vgpu_count":10,"running_training_count":10}},"insufficient_instance_slots")])
def test_hard_constraints_filter_before_model(runtime,gpus,change,reason,monkeypatch):
    g = deepcopy(gpus[0])
    for key,value in change.items():
        if key == "dynamic":
            g["dynamic"].update(value)
        else:
            g[key] = value
    monkeypatch.setattr(runtime,"classify_gpu",lambda *_:pytest.fail("Rejected GPU reached classifier"))
    r = runtime.rank_gpus(pod(),[g])
    assert r["recommendation"] is None and reason in r["rejected_gpus"][0]["rejection_reasons"]


def test_full_core_exclusivity_and_zero_core_native_semantics(runtime,gpus):
    pool = GPUPool(runtime,[gpus[0]])
    p = pod("unrestricted",cores=0,memory=1000)
    held = pool.reserve_best(p)["reservation"]
    assert pool.snapshot()[0]["dynamic"]["free_compute_quota"] == 100
    assert runtime.classify_gpu(pool.snapshot()[0])["prediction_status"] == "pending_verification"
    r = runtime.rank_gpus(pod("exclusive",cores=100),pool.snapshot())
    assert "full_core_request_requires_empty_GPU" in r["rejected_gpus"][0]["rejection_reasons"]
    pool.release(held["reservation_id"])
    held = pool.reserve_best(pod("exclusive",cores=100))["reservation"]
    r = runtime.rank_gpus(pod("zero",cores=0,memory=1000),pool.snapshot())
    assert r["recommendation"] is None
    pool.release(held["reservation_id"])


def test_default_model_frozen_coverage_and_known_classification(runtime,gpus):
    assert runtime.model_error is None
    assert runtime.manifest["selected_protocol"] == "known_states"
    assert set(("rtx4090","rtx5090","l40")) <= set(runtime.model.metadata["train_device_ids"])
    for g in gpus:
        c = runtime.classify_gpu(g)
        assert c["prediction_status"] == "provisional" and c["raw_rf"]
        assert c["model_version"] == runtime.version and not c["real_performance_guarantee"]


def test_unknown_numeric_profile_and_OOD_safely_fall_back(runtime,gpus):
    g = deepcopy(gpus[0]); g.update(device_id="future-unknown",model="future-GPU",architecture="unseen")
    assert runtime.classify_gpu(g)["raw_rf"] is not None
    g["hardware"]["fp32_tflops"] = 9999
    c = runtime.classify_gpu(g)
    assert c["prediction_status"] == "pending_verification" and c["raw_rf"] is not None
    r = runtime.rank_gpus(pod(),[g])["eligible_gpus"][0]
    assert r["ranking_basis"] == "deterministic_conservative_proxy" and r["fallback_score"] is not None
    assert r["rf_probability"] is not None and not r["fallback_score_is_probability"]


def test_missing_performance_spec_allows_resource_fit_but_not_RF_certainty(runtime,gpus):
    g = deepcopy(gpus[0]); g["hardware"]["fp32_tflops"] = None
    assert runtime.classify_gpu(g)["prediction_status"] == "pending_verification"
    r = runtime.rank_gpus(pod(),[g])
    assert r["recommendation"] is not None
    assert r["eligible_gpus"][0]["ranking_basis"] == "deterministic_conservative_proxy"


def test_dynamic_allocate_release_and_independent_same_model_devices(runtime,gpus):
    fleet = [g for g in gpus if g["device_id"] == "l40"]
    pool = GPUPool(runtime,fleet)
    original = pool.snapshot()
    before = runtime.classify_gpu(original[0])
    result = pool.reserve_best(pod("compute",memory=1000,cores=60))
    key = result["reservation"]["assignments"][0]["gpu_uuid"]
    now = next(g for g in pool.snapshot() if g["uuid"] == key)
    assert now["dynamic"]["free_compute_quota"] == 40
    assert now["dynamic"]["running_training_pod_count"] == 1
    after = runtime.classify_gpu(now)
    assert after["label"] == "training_preferred" and after["input_sha256"] != before["input_sha256"]
    assert all(g["dynamic"]["free_compute_quota"] == 100 for g in pool.snapshot() if g["uuid"] != key)
    pool.release("compute")
    restored = next(g for g in pool.snapshot() if g["uuid"] == key)
    assert restored["hardware"] == original[0]["hardware"]
    assert runtime.classify_gpu(restored)["raw_rf"] == before["raw_rf"]
    assert not pool.release("compute")["released"]


def test_multi_device_same_node_distinct_per_container_and_atomic_restore(runtime,gpus):
    pool = GPUPool(runtime,gpus)
    p = pod("two",count=2)
    result = pool.reserve_best(p)
    assigns = result["reservation"]["assignments"]
    assert len({a["gpu_uuid"] for a in assigns}) == 2 and len({a["node"] for a in assigns}) == 1
    pool.commit("two",[a["gpu_uuid"] for a in assigns])
    pool.release("two")
    assert all(g["dynamic"]["allocated_vgpu_count"] == 0 for g in pool.snapshot())


def test_multiple_containers_share_one_GPU_but_count_one_Pod(runtime,gpus):
    p = pod("two-containers",memory=1000,cores=10)
    other = deepcopy(p["spec"]["containers"][0]); other["name"] = "second"
    p["spec"]["containers"].append(other)
    pool = GPUPool(runtime,[gpus[0]])
    assert pool.reserve_best(p)["status"] == "reserved"
    d = pool.snapshot()[0]["dynamic"]
    assert d["allocated_vgpu_count"] == d["running_training_count"] == 2
    assert d["running_training_pod_count"] == 1
    pool.release("two-containers")
    assert pool.snapshot()[0]["dynamic"]["running_training_pod_count"] == 0


def test_multi_device_cannot_cross_nodes(runtime,gpus):
    one_each = [gpus[0],gpus[4],gpus[8]]
    result = runtime.rank_gpus(pod(count=2),one_each)
    assert result["recommendation"] is None and result["status"] == "unschedulable"


def test_model_load_failure_preserves_hard_constraints(tmp_path,runtime,gpus):
    failed = SuitabilityService(tmp_path/"missing")
    assert failed.model is None and failed.model_error
    result = failed.rank_gpus(pod(),gpus)
    assert result["recommendation"] and all(r["rf_probability"] is None for r in result["eligible_gpus"])
    assert failed.rank_gpus(pod(memory=999999),gpus)["recommendation"] is None


def test_corrupt_frozen_model_is_not_loaded(tmp_path,gpus):
    target = tmp_path/"corrupt-bundle"
    shutil.copytree(DEFAULT_BUNDLE,target)
    (target/"random_forest.joblib").write_bytes(b"not-a-model")
    failed = SuitabilityService(target)
    assert "checksum" in failed.model_error and failed.model is None
    assert failed.rank_gpus(pod(),gpus)["recommendation"]


def test_reference_unavailable_is_not_pod_admission_rule(runtime,gpus):
    g = deepcopy(gpus[0])
    g["dynamic"].update(free_memory_mib=1000,free_compute_quota=5,allocated_vgpu_count=2,running_training_count=2)
    assert runtime.classify_gpu(g)["label"] == "unavailable"
    r = runtime.rank_gpus(pod(memory=512,cores=1),[g])
    assert r["recommendation"] and r["eligible_gpus"][0]["feasible"]


def test_no_runtime_fit_and_stable_recommendations(runtime,gpus,monkeypatch):
    from sklearn.ensemble import RandomForestClassifier
    monkeypatch.setattr(RandomForestClassifier,"fit",lambda *_a,**_k:pytest.fail("Runtime tried to train"))
    loaded = SuitabilityService()
    assert loaded.rank_gpus(pod(),gpus) == loaded.rank_gpus(pod(),gpus)
    assert classify_gpu(gpus[0])["model_version"] == runtime.version
    assert rank_gpus(pod(),gpus)["recommendation"]


def test_stale_plan_health_changes_and_uuid_mismatch_rollback(runtime,gpus):
    pool = GPUPool(runtime,[gpus[0]])
    p = pod("old")
    plan = runtime.rank_gpus(p,pool.snapshot())["recommendation"]
    pool.reserve_best(pod("new",cores=60))
    with pytest.raises(ReservationConflict,match="stale"):
        pool.reserve(p,plan)
    pool.release("new")
    held = pool.reserve_best(p)["reservation"]
    with pytest.raises(ReservationConflict,match="UUID|uuid"):
        pool.commit(held["reservation_id"],["wrong-uuid"])
    assert pool.snapshot()[0]["dynamic"]["allocated_vgpu_count"] == 0
    held = pool.reserve_best(p)["reservation"]
    pool.update_health(gpus[0]["uuid"],False)
    with pytest.raises(ReservationConflict,match="revalidation"):
        pool.commit(held["reservation_id"],[gpus[0]["uuid"]])
    assert pool.snapshot()[0]["dynamic"]["allocated_vgpu_count"] == 0


def test_simultaneous_reservations_never_overallocate(runtime,gpus):
    pool = GPUPool(runtime,[gpus[0]])
    barrier = threading.Barrier(2)
    def attempt(i):
        p = pod(f"parallel-{i}",memory=4000,cores=60)
        plan = runtime.rank_gpus(p,pool.snapshot())["recommendation"]
        barrier.wait(timeout=10)
        try:
            pool.reserve(p,plan)
            return True
        except ReservationConflict:
            return False
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(attempt,range(2)))
    assert sum(results) == 1
    assert pool.snapshot()[0]["dynamic"]["free_compute_quota"] == 40
    assert pool.snapshot()[0]["dynamic"]["allocated_vgpu_count"] == 1


def test_expire_uncommitted_reservations_and_idempotency(runtime,gpus):
    pool = GPUPool(runtime,[gpus[0]],reservation_timeout_s=1)
    p = pod("expire")
    result = pool.reserve_best(p)
    duplicate = pool.reserve(p,result["ranking"]["recommendation"])
    assert duplicate["status"] == "already_reserved"
    assert len(pool.expire(time.monotonic()+10)) == 1
    assert pool.snapshot()[0]["dynamic"]["free_compute_quota"] == 100


def test_advisory_timeout_and_json_dispatch(runtime,gpus):
    class Timeout:
        def rank_gpus(self,*_):
            raise TimeoutError("offline injected deadline")
    pool = GPUPool(runtime,gpus)
    bridge = MockVolcanoAdapter(runtime,pool,adviser=Timeout())
    plan = bridge.plan(pod())
    assert "TimeoutError" in plan["adviser_failure"]
    assert all(r["ranking_basis"] == "deterministic_conservative_proxy" for r in plan["eligible_gpus"])
    allocated = bridge.allocate(pod(),plan)
    assert allocated["commit"]["native_uuid_match"]
    pool.release("test")
    result = dispatch(runtime,{"schema_version":"pod-gpu-api/v1","method":"rank_gpus","pod":pod(),"gpus":gpus})
    assert result["recommendation"]
    assert dispatch(runtime,{"schema_version":"bad"})["status"] == "invalid_input"


def test_foreign_UUID_advice_is_discarded_before_reservation(runtime,gpus):
    class WrongUUID:
        def rank_gpus(self,p,snapshots):
            r = runtime.rank_gpus(p,snapshots)
            rec = r["recommendation"]
            rec["assignments"][0]["gpu_uuid"] = "foreign-UUID"
            rec.pop("selection_fingerprint")
            rec["selection_fingerprint"] = digest(rec)
            return r
    pool = GPUPool(runtime,gpus)
    bridge = MockVolcanoAdapter(runtime,pool,adviser=WrongUUID())
    r = bridge.plan(pod())
    assert "unknown_UUID" in r["adviser_failure"]
    assert r["recommendation"]["gpu_uuid"] in {g["uuid"] for g in gpus}
    assert all(g["dynamic"]["allocated_vgpu_count"] == 0 for g in pool.snapshot())


def test_real_json_lines_worker_uses_UTF8_and_handles_multiple_requests(runtime,gpus):
    requests = [{"schema_version":"pod-gpu-api/v1","method":"classify_gpu","gpu":gpus[0]},
                {"schema_version":"pod-gpu-api/v1","method":"rank_gpus","pod":pod(kind="inference"),"gpus":gpus}]
    process = subprocess.run([sys.executable,"-m","gpu_lab.runtime.cli","json-lines"],cwd=ROOT,
                             input="\n".join(json.dumps(r,ensure_ascii=False) for r in requests)+"\n",capture_output=True,
                             encoding="utf-8",timeout=30)
    assert process.returncode == 0,process.stderr
    replies = [json.loads(line) for line in process.stdout.splitlines()]
    assert len(replies) == 2 and replies[0]["label_zh"] == "训推一体型"
    assert replies[1]["recommendation"]["gpu_uuid"] in {g["uuid"] for g in gpus}


@pytest.mark.parametrize("strategy",["binpack","static_rf","dynamic_rf"])
def test_runtime_api_inside_old_simulation(strategy,runtime):
    profiles = load_profiles()
    fleet = create_fleet(profiles)
    result = simulate(generate_trace("bursty_mixed",7,12),profiles,RuntimeScheduler(strategy,runtime),ExecutionModel(seed=707),fleet=fleet)
    assert result["metrics"]["completed_total_after_drain"] == 12
    assert all(not s.allocations and s.remaining_compute_units == 100 for s in fleet.values())
    assert sum(e.get("state_prediction_after") is not None for e in result["events"]) == 24
