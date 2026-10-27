from ..crossgpu.adapter import LegacyAdapter
from ..crossgpu.demos import dynamic_demo
from ..crossgpu.features import initial_state
from ..execution import ExecutionModel
from ..fleet import create_fleet
from ..pods import TASK_LABEL, task_type_from_pod
from ..profiles import load_profiles
from ..resources import ResourceRequest
from ..scoring import PodRequest, packing_residual
from ..simulation import simulate
from ..strategies import Scheduler, STRATEGIES
from ..workloads import generate_trace
from ..crossgpu.theory import UNKNOWN
from .model import probabilities


class ConservativeAdapter(LegacyAdapter):
    """Pending RF predictions use deterministic theoretical hints, never admission."""
    def predict_many(self, ledgers):
        result = super().predict_many(ledgers)
        for row in result:
            if row["label"] == UNKNOWN:
                details = row["fallback"].get("details", {})
                t = details.get("training", {}).get("theoretical_suitability_score", 0.)
                i = details.get("inference", {}).get("theoretical_suitability_score", 0.)
                row["class_probabilities"] = probabilities(t, i)
                row["class_probability_basis"] = "legacy ranking interface carrying deterministic conservative scores, NOT classification probabilities"
                row["ranking_fallback"] = True
        return result


def conservative_ranking(model, devices, ledgers, pod):
    rows = []
    adapter = ConservativeAdapter(model, devices)
    for ledger in ledgers:
        if ledger.profile.profile_id not in devices or devices[ledger.profile.profile_id].record["software_backend"] != "CUDA":
            rows.append({"gpu_id":ledger.gpu_id,"feasible":False,"reason":"unverified software backend"})
            continue
        if not ledger.can_allocate(pod.request):
            rows.append({"gpu_id":ledger.gpu_id,"feasible":False,"reason":"memory/compute/instance hard constraints"})
            continue
        p = adapter.predict(ledger)
        score = p["class_probabilities"][pod.task_type+"_preferred"] + p["class_probabilities"]["balanced"]
        rows.append({"gpu_id":ledger.gpu_id,"feasible":True,"score":score,"packing_residual":packing_residual(ledger,pod.request),
                     "basis":"conservative_theory" if p.get("ranking_fallback") else "RF_soft_hint", "prediction":p})
    return sorted(rows,key=lambda r:(not r["feasible"],-r.get("score",0),r.get("packing_residual",0),r["gpu_id"]))


def demonstrations(model, devices):
    result = {"dynamic":{k:dynamic_demo(model,devices[k]) for k in ("rtx4090","rtx5090","l40")},
              "unknown":[{"device_id":k,"hardware":devices[k].record,"state":initial_state(devices[k]),
                           "prediction":model.predict_device(devices[k],initial_state(devices[k])),
                           "performance_ground_truth":"unknown", "not_used_in_real_performance_accuracy":True}
                         for k in ("rtx_a6000","rtx6000_ada","rtx5080","a10","h200_sxm")]}
    profiles = load_profiles()
    fleet = create_fleet(profiles)
    result["fallback_pod_ranking"] = conservative_ranking(model,devices,list(fleet.values()),PodRequest("training", ResourceRequest(8192,40)))
    return result


def regression(model, devices, count=60):
    profiles, trace = load_profiles(), generate_trace("bursty_mixed",101,count)
    summaries, audits, events = [], {}, {}
    for kind in ("training","inference"):
        assert task_type_from_pod({"kind":"Pod","metadata":{"labels":{TASK_LABEL:kind}}}) == kind
    for name in STRATEGIES:
        fleet = create_fleet(profiles)
        scheduler = Scheduler(name,profiles,static_model=ConservativeAdapter(model,devices,static=True),dynamic_model=ConservativeAdapter(model,devices))
        result = simulate(trace,profiles,scheduler,ExecutionModel(seed=707),fleet=fleet)
        for event in result["events"]:
            if "state_after" in event:
                s = event["state_after"]
                assert 0 <= s["remaining_memory_mib"] <= s["capacity_memory_mib"]
                assert 0 <= s["remaining_compute_units"] <= 100
                assert 0 <= s["allocated_instances"] <= 10
        assert all(not s.allocations and s.remaining_compute_units == 100 and s.remaining_memory_mib == s.profile.modeled_memory_mib for s in fleet.values())
        assert result["metrics"]["completed_total_after_drain"] == count
        summaries.append(result["metrics"])
        events[name] = result["events"]
        audits[name] = {"completed":count,"no_overallocation":True,"all_resources_restored":True,
                        "state_changes":sum(e["event"] in ("start","finish") for e in result["events"]),
                        "predictions_after_change":sum(e.get("state_prediction_after") is not None for e in result["events"])}
    assert len({m["trace_sha256"] for m in summaries}) == 1
    return {"summary":summaries,"audit":audits,"events":events,"pod_label_checks":True,
            "scope":"4090x4+5090x4+L40x3 compatibility regression only, old independent NVIDIA synthetic execution model"}
