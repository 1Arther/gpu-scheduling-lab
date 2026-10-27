from copy import deepcopy
from urllib.parse import urlparse

import numpy as np
import pytest

from gpu_lab.crossgpu.catalog import Device, REQUIRED, STATIC
from gpu_lab.crossgpu.data import audit, generate
from gpu_lab.crossgpu.features import features as original_features, initial_state
from gpu_lab.crossgpu.theory import UNKNOWN, assess
from gpu_lab.nvidia.data import assemble_catalog, load_plan, split_protocol
from gpu_lab.nvidia.demos import ConservativeAdapter, conservative_ranking, regression
from gpu_lab.nvidia.features import EFFECTIVE, FEATURE_SETS, enrich, features
from gpu_lab.nvidia.model import NvidiaModel, probabilities, select_models
from gpu_lab.profiles import load_profiles
from gpu_lab.resources import GPUState, ResourceRequest
from gpu_lab.scoring import PodRequest


@pytest.fixture(scope="module")
def nv_catalog(tmp_path_factory):
    return assemble_catalog(tmp_path_factory.mktemp("nv4")/"source.json")[0]


@pytest.fixture(scope="module")
def nv_rows(nv_catalog):
    return generate(nv_catalog,seed=404,episodes=20,steps=12)[0]


@pytest.fixture(scope="module")
def nv_fitted(nv_catalog,nv_rows):
    plan = deepcopy(load_plan())
    plan["training"].update(trees=20,min_samples_leaf=[2])
    plan["feature_sets"] = ["baseline14","effective_both"]
    splits = split_protocol(nv_rows,plan["protocols"]["known_states"],plan["seed"])
    models,selected,_ = select_models(splits,nv_catalog,plan)
    return models[selected]


def test_fourth_round_catalog_reuses_distinct_official_configurations(nv_catalog):
    assert len(nv_catalog) == 15 and all(d.eligible() for d in nv_catalog.values())
    assert {d.record["vendor"] for d in nv_catalog.values()} == {"NVIDIA"}
    assert len({d.record["architecture"] for d in nv_catalog.values()}) == 4
    assert nv_catalog["a40"].values()["fp32_tflops"] == 37.4
    assert nv_catalog["rtx5080"].values()["fp32_tflops"] == 56.3
    assert nv_catalog["rtx5080"].values()["memory_capacity_mib"] == int(16e9/2**20)
    assert nv_catalog["rtx_a6000"].values()["fp16_dense_tflops"] is None
    assert nv_catalog["rtx6000_ada"].values()["int8_dense_tops"] is None
    for d in nv_catalog.values():
        assert tuple(d.values()) == STATIC
        for s in d.record["specifications"].values():
            assert urlparse(s["source_url"]).hostname.endswith("nvidia.com") and s["locator"] and s["raw_unit"]
        assert all(d.values(comparable=True)[k] is None for k in STATIC[3:6])


def test_fixed_fresh_test_models_never_fit_or_validate_any_protocol(nv_catalog,nv_rows):
    plan = load_plan()
    for name,config in plan["protocols"].items():
        split = split_protocol(nv_rows,config,plan["seed"])
        assert not set(plan["fresh_test_models"]) & {r["device_id"] for r in split["train"]+split["validation"]}
        checked = audit(split,model_disjoint=config["kind"] != "known_model_unseen_episode")
        assert not any(checked["overlaps"].values())
        if name.startswith("A_"):
            assert len({nv_catalog[r["device_id"]].record["architecture"] for batch in split.values() for r in batch}) == 1
        if name == "C_blackwell_holdout":
            assert all(nv_catalog[r["device_id"]].record["architecture"] != "Blackwell" for r in split["train"]+split["validation"])
            assert all(nv_catalog[r["device_id"]].record["architecture"] == "Blackwell" for r in split["test"])


def test_feature_products_are_input_only_and_do_not_mutate_hardware(nv_catalog):
    d = nv_catalog["rtx4090"]
    s = initial_state(d,semantics="synthetic_device_share_100_v1")
    s["free_compute_quota"] = 40
    old = deepcopy(d.record)
    x = features(d,s)
    assert x["effective_fp32_tflops"] == pytest.approx(82.6*.4)
    assert x["effective_bandwidth_gbps"] == pytest.approx(1008*.4)
    assert x["compute_bandwidth_flop_per_byte"] == pytest.approx(82.6*1000/1008)
    assert d.record == old
    original = original_features(d,s)
    assert {k:x[k] for k in original} == original
    assert all("label" not in k and "architecture" not in k for k in x)
    assert "free_memory_mib" not in FEATURE_SETS["relative_memory"]
    assert "memory_capacity_mib" not in FEATURE_SETS["relative_memory"]


def test_name_and_architecture_not_RF_inputs(nv_catalog,nv_fitted):
    d = nv_catalog["l40"]
    clone = deepcopy(d.record)
    clone.update(device_id="never-seen-id",model="unknown-name",architecture=None)
    a,b = features(d,initial_state(d)),features(Device(clone),initial_state(d))
    assert a == b
    assert nv_fitted.raw_predict([a]) == nv_fitted.raw_predict([b])
    result = nv_fitted.predict_device(Device(clone),initial_state(d))
    assert result["raw_rf"] is not None
    assert "architecture" not in nv_fitted.metadata["feature_names"]


def test_RF_serialization_and_probability_scope(tmp_path,nv_catalog,nv_fitted):
    path = tmp_path/"nvidia.joblib"
    nv_fitted.save(path)
    loaded = NvidiaModel.load(path)
    d = nv_catalog["l40"]
    x = features(d,initial_state(d))
    assert loaded.raw_predict([x]) == nv_fitted.raw_predict([x])
    p = loaded.raw_predict([x])[0]
    assert sum(p["class_probabilities"].values()) == pytest.approx(1)
    assert all(0 <= v <= 1 for v in p["class_probabilities"].values())
    assert not p["vote_fraction_is_reliability"]


def strong_local_model(nv_fitted,nv_catalog):
    model = NvidiaModel(nv_fitted.pipeline,deepcopy(nv_fitted.metadata))
    d = nv_catalog["l40"]
    cells = {target:{str(sign):{"support":30,"correct":30,"episodes":5} for sign in (0,1)} for target in ("training","inference")}
    model.metadata["validation_devices"] = [{"device_id":"local-validation-unit-fixture","architecture":d.record["architecture"],
                                             "hardware":{k:d.values()[k] for k in REQUIRED},"cells":cells}]
    return model


def test_missing_combined_validation_class_is_not_global_rejection(nv_catalog,nv_fitted):
    model = strong_local_model(nv_fitted,nv_catalog)
    model.metadata["validation_four_class_counts"] = {"balanced":30,"inference_preferred":30,"unavailable":30}
    model.metadata["validation_has_all_four_classes"] = False
    d = nv_catalog["l40"]
    assert model.predict_device(d,initial_state(d))["label"] != UNKNOWN


def test_high_RF_vote_does_not_override_bad_local_validation(nv_catalog,nv_fitted):
    model = strong_local_model(nv_fitted,nv_catalog)
    model.metadata["validation_devices"][0]["cells"]["training"]["1"]["correct"] = 0
    raw = {"training":True,"inference":True,"label":"balanced","training_vote_fraction":1.,"inference_vote_fraction":1.,"class_probabilities":probabilities(1.,1.)}
    d = nv_catalog["l40"]
    p = model.predict_device(d,initial_state(d),raw_prediction=raw)
    assert p["label"] == UNKNOWN
    assert "insufficient_related_validation_for_training_sign_1" in p["reasons"]


def test_missing_related_class_does_not_block_hard_resource_negative(nv_catalog,nv_fitted):
    model = strong_local_model(nv_fitted,nv_catalog)
    model.metadata["validation_devices"] = []
    d = nv_catalog["l40"]
    s = initial_state(d,"synthetic_device_share_100_v1")
    s.update(free_memory_mib=100,free_compute_quota=1)
    p = model.predict_device(d,s)
    assert p["label"] == "unavailable" and not p["training"] and not p["inference"]
    assert p["target_validation_evidence"]["training"]["basis"] == "deterministic_reference_resource_filter"


def test_OOD_new_GPU_remains_pending_even_if_raw_correct(nv_catalog,nv_fitted):
    d = nv_catalog["rtx5080"]
    p = nv_fitted.predict_device(d,initial_state(d))
    assert p["label"] == UNKNOWN and "outside_training_static_core_range" in p["reasons"]
    assert "memory_capacity_mib" in p["outside_training_ranges"]
    assert p["fallback"]["conservative_factor"] == .8


@pytest.mark.parametrize("case",["missing_fp32","invalid_memory","unmapped_quota","unknown_backend"])
def test_bad_input_abstains_before_RF(nv_catalog,nv_fitted,case,monkeypatch):
    d = deepcopy(nv_catalog["l40"].record)
    if case == "missing_fp32":
        d["specifications"]["fp32_tflops"]["value"] = None
    if case == "unknown_backend":
        d["software_backend"] = "unverified"
    device = Device(d)
    state = initial_state(device)
    if case == "invalid_memory":
        state["free_memory_mib"] = -1
    if case == "unmapped_quota":
        state["quota_semantics"] = "native_cores_unmapped"
    monkeypatch.setattr(nv_fitted,"raw_predict",lambda *_:pytest.fail("Invalid input reached RF"))
    assert nv_fitted.predict_device(device,state)["label"] == UNKNOWN


def test_validation_selection_never_reads_test_and_is_reproducible(nv_catalog,nv_rows):
    plan = deepcopy(load_plan())
    plan["training"].update(trees=12,min_samples_leaf=[2])
    plan["feature_sets"] = ["effective_both"]
    split = split_protocol(nv_rows,plan["protocols"]["B_multi_architecture"],plan["seed"])
    class Forbidden:
        def __iter__(self):
            pytest.fail("Test read during fitting/feature selection")
    safe = {**split,"test":Forbidden()}
    a,selected,ca = select_models(safe,nv_catalog,plan)
    b,other,cb = select_models(safe,nv_catalog,plan)
    assert selected == other and ca == cb
    x = [r["features"] for r in split["validation"]]
    assert a[selected].raw_predict(x) == b[other].raw_predict(x)
    assert a[selected].metadata["test_used_for_selection"] is False


def test_pending_predictions_use_conservative_ranking_after_hard_filters(nv_catalog,nv_fitted):
    profiles = load_profiles()
    s = GPUState("l40",profiles["l40"])
    model = NvidiaModel(nv_fitted.pipeline,deepcopy(nv_fitted.metadata))
    model.metadata["validation_devices"] = []
    p = ConservativeAdapter(model,nv_catalog).predict(s)
    assert p["label"] == UNKNOWN and p["ranking_fallback"]
    assert "NOT classification probabilities" in p["class_probability_basis"]
    rank = conservative_ranking(model,nv_catalog,[s],PodRequest("training",ResourceRequest(8192,40)))
    assert rank[0]["feasible"] and rank[0]["basis"] == "conservative_theory"
    impossible = conservative_ranking(model,nv_catalog,[s],PodRequest("training",ResourceRequest(999999,40)))
    assert not impossible[0]["feasible"]


def test_fourth_round_three_strategy_regression(nv_catalog,nv_fitted):
    r = regression(nv_fitted,nv_catalog,count=12)
    assert r["pod_label_checks"]
    assert len({m["trace_sha256"] for m in r["summary"]}) == 1
    for name,audit_result in r["audit"].items():
        assert audit_result["completed"] == 12 and audit_result["all_resources_restored"] and audit_result["no_overallocation"]
        if name == "dynamic_rf":
            assert audit_result["predictions_after_change"] == 24
