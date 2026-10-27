"""CPU-only frozen-model verification; no fit, SSH or Kubernetes operations."""
from copy import deepcopy
import csv
import json

import pytest
import yaml

from gpu_lab.round8 import (ROOT, OUT, PLAN, MODEL_SHA, sha, pod, fixture_gpu,
                            run_path, run_controlled_fixture, flat_state)
from gpu_lab.runtime.service import SuitabilityService
from gpu_lab.runtime.pods import parse_pod
from gpu_lab.runtime.schema import gpu_errors, model_state


@pytest.fixture(scope="module")
def service():
    s=SuitabilityService()
    assert s.model is not None
    return s


@pytest.fixture(scope="module")
def path(service):
    return run_path(service)


def test_frozen_artifacts_unchanged():
    audit=json.loads((OUT/"frozen-artifact-audit.json").read_text(encoding="utf-8"))
    assert audit["before"]==audit["after"]
    assert sha(ROOT/"models/pod-gpu-rf-v1/random_forest.joblib")==MODEL_SHA
    for name,expected in audit["before"].items():
        assert sha(ROOT/"models/pod-gpu-rf-v1"/name)==expected


def test_real_frozen_RF_four_class_path(path):
    assert [s["classification"]["raw_rf"]["label"] for s in path["states"]]==[
        "balanced","training_preferred","unavailable","inference_preferred","balanced"]
    assert all(s["classification"]["prediction_status"]=="provisional" for s in path["states"])
    assert all(s["classification"]["raw_rf"]["label"]==s["classification"]["label"] for s in path["states"])
    assert all(not s["classification"]["real_performance_guarantee"] for s in path["states"])


def test_quota_path_and_release_no_hardware_change(path):
    assert [(s["gpu"]["dynamic"]["free_memory_mib"],s["gpu"]["dynamic"]["free_compute_quota"],
             s["gpu"]["dynamic"]["allocated_vgpu_count"]) for s in path["states"]]==[
        (24564,100,0),(23564,40,1),(6000,35,2),(7000,95,1),(24564,100,0)]
    assert all(not gpu_errors(s["gpu"]) for s in path["states"])
    assert all(s["gpu"]["hardware"]==path["states"][0]["gpu"]["hardware"] for s in path["states"])
    assert path["states"][0]["gpu"]["dynamic"]==path["states"][-1]["gpu"]["dynamic"]
    assert path["ledger_restored"]


def test_mapping_preserves_logical_mib(path):
    for s in path["states"]:
        g=s["gpu"]
        assert g["logical_memory_capacity_mib"]==24564
        assert model_state(g)["free_memory_mib"]==g["dynamic"]["free_memory_mib"]*22888//24564
        assert flat_state(s)["native_allocate_uuid"] is None


def test_classifier_is_not_pod_admission(service,path):
    g=path["states"][2]["gpu"]
    assert service.classify_gpu(g)["label"]=="unavailable"
    small=pod("probe","inference",64,1)
    assert service.rank_gpus(small,[g])["recommendation"] is not None
    too_big=pod("cannot-fit","training",7000,20)
    result=service.rank_gpus(too_big,[g])
    assert result["recommendation"] is None
    assert "insufficient_memory" in " ".join(result["rejected_gpus"][0]["rejection_reasons"])


def test_full_core_constraints_same_device_control(service):
    result=run_controlled_fixture(service)
    assert len(set(result["blocker_uuids"]))==3
    assert result["target_uuid"] not in result["blocker_uuids"]
    assert result["all_restored"]
    classes={s["stage"]:s["classification"]["label"] for s in result["steps"]}
    assert classes["after_memory"]=="unavailable"
    assert classes["release_compute"]=="inference_preferred"
    assert classes["release_all"]=="balanced"
    assert classes["after_memory_observer_allocated"]=="unavailable"
    assert classes["release_compute_observer_allocated"]=="inference_preferred"
    assert classes["release_all_observer_allocated"]=="balanced"


def test_reproducible_state_and_scores(service,path):
    repeated=run_path(service)
    assert [flat_state(s) for s in repeated["states"]]==[flat_state(s) for s in path["states"]]


def test_manifests_are_sleep_only_no_forged_UUID():
    paths=list(PLAN.glob("*.yaml"))
    assert len(paths)==8
    for file in paths:
        pg,p=yaml.safe_load_all(file.read_text(encoding="utf-8"))
        assert pg["spec"]["minMember"]==1
        assert "uid" not in p["metadata"]
        assert p["spec"]["containers"][0]["command"]==["/bin/sleep","600"]
        assert p["spec"]["schedulerName"]=="volcano-rf-test"
        assert set(p["metadata"]["annotations"])=={"scheduling.k8s.io/group-name"}
        parsed=parse_pod(p)
        assert parsed.task_type in ("training","inference")
        assert len(parsed.requests)==1 and parsed.requests[0].count==1
    gate=json.loads((PLAN/"execution-gate.json").read_text())
    assert gate["approved"] is False and gate["real_cluster_operations_allowed"] is False


def test_exact_peak_quota():
    hard=json.loads((PLAN/"quota-spec-merge-patch.json").read_text())["spec"]["hard"]
    assert int(hard["pods"])==3+2+1
    assert int(hard["requests.volcano.sh/vgpu-memory"])==3*64+1000+17564+64
    assert int(hard["requests.volcano.sh/vgpu-cores"])==3*100+60+5+1


def test_historical_evidence_consistency():
    material=json.loads((OUT/"material-validation.json").read_text())
    assert material["historical_simulation_recomputed_and_matches"]
    assert material["ablation_cases"]==180 and material["rf_uuid_changes"]==37
    assert material["real_native_plugin_matches"]==6
    assert material["real_rf_native_plugin_eligible"]==5
    assert not material["real_dynamic_class_change_demonstrated"]
    assert not material["new_cluster_access"]


def test_grid_does_not_hide_unrestricted_quota():
    with (OUT/"phase-grid.csv").open(encoding="utf-8-sig") as f:rows=list(csv.DictReader(f))
    assert len(rows)==540
    boundary=[r for r in rows if r["free_compute_quota"]=="100"]
    assert len(boundary)==27
    assert all(r["unrestricted_vgpu_count"]=="1" and r["prediction_status"]=="pending_verification" for r in boundary)
