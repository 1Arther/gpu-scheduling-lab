"""Synthetic static/dynamic/unknown demonstrations and a legacy regression replay."""
from copy import deepcopy
import hashlib
import json

from ..execution import ExecutionModel
from ..fleet import create_fleet
from ..profiles import load_profiles
from ..resources import Allocation, GPUState, ResourceRequest
from ..simulation import simulate
from ..strategies import Scheduler, STRATEGIES
from ..workloads import generate_trace, trace_hash
from .adapter import LegacyAdapter
from .catalog import Device
from .features import from_ledger, initial_state
from .theory import assess


def static_demo(model, devices):
    return [{"device_id": d.device_id, "hardware": d.record, "state": initial_state(d),
             "prediction": model.predict_device(d, initial_state(d)),
             "independent_reference_rule": assess(d, initial_state(d)),
             "ground_truth_performance_label": "unknown"} for d in devices.values()]


def dynamic_demo(model, device):
    ledger, history = GPUState("round3-dynamic-l40", device), []
    original = hashlib.sha256(json.dumps(device.record, sort_keys=True).encode()).hexdigest()

    def snapshot(event):
        state = from_ledger(ledger)
        hardware_hash = hashlib.sha256(json.dumps(device.record, sort_keys=True).encode()).hexdigest()
        if hardware_hash != original:
            raise AssertionError("Allocation changed static hardware")
        history.append({"event": event, "device_id": device.device_id, "model": device.record["model"],
                        "hardware": device.values(), "hardware_sha256": hardware_hash, "state": state,
                        "prediction": model.predict_device(device, state), "independent_reference_rule": assess(device, state),
                        "data_kind": "constructed_synthetic_resource_trajectory_not_a_performance_test"})
    snapshot("initial")
    ledger.allocate(Allocation("compute-heavy", "training", ResourceRequest(1000, 60)))
    snapshot("allocate_compute_heavy_1000MiB_60quota")
    ledger.allocate(Allocation("memory-heavy", "inference", ResourceRequest(device.modeled_memory_mib - 7000, 5)))
    snapshot("allocate_memory_heavy_5quota")
    ledger.release("compute-heavy")
    snapshot("release_compute_heavy")
    ledger.release("memory-heavy")
    snapshot("release_all")
    return history


def unknown_demo(model, devices):
    examples = []
    for name in ("a10", "mi300x", "mtt_s4000", "intel_max1550", "mlu370_x8", "ascend910", "biren166c"):
        device = devices[name]
        examples.append({"case": name, "hardware": device.record, "state": initial_state(device),
                         "prediction": model.predict_device(device, initial_state(device)),
                         "ground_truth_performance_label": "unknown", "included_in_real_accuracy": False})
    rec = deepcopy(devices["a10"].record)
    rec.update(device_id="unseen_architecture_numeric_probe", model="unseen name, same public A10 numerical specs", architecture="never-seen-architecture")
    device = Device(rec)
    examples.append({"case": "identity_and_architecture_rename", "hardware": device.record,
                     "prediction": model.predict_device(device, initial_state(device)),
                     "data_kind": "synthetic metadata probe, not a newly discovered GPU", "included_in_real_accuracy": False})
    rec = deepcopy(devices["a10"].record)
    rec["specifications"]["fp32_tflops"]["value"] = None
    device = Device(rec)
    examples.append({"case": "missing_required_FP32", "prediction": model.predict_device(device, initial_state(device)),
                     "data_kind": "synthetic missingness probe", "included_in_real_accuracy": False})
    state = initial_state(devices["a10"], semantics="unknown_vendor_native_cores")
    examples.append({"case": "unmapped_native_quota", "prediction": model.predict_device(devices["a10"], state),
                     "data_kind": "synthetic quota semantics probe", "included_in_real_accuracy": False})
    return examples


def regression(model, devices, seed=101, count=60):
    profiles = load_profiles()
    trace = generate_trace("bursty_mixed", seed, count)
    dynamic, static = LegacyAdapter(model, devices), LegacyAdapter(model, devices, static=True)
    results, summaries, audit = {}, [], {}
    for name in STRATEGIES:
        fleet = create_fleet(profiles)
        scheduler = Scheduler(name, profiles, static_model=static, dynamic_model=dynamic)
        result = simulate(trace, profiles, scheduler, ExecutionModel(seed=707), fleet=fleet)
        if result["metrics"]["trace_sha256"] != trace_hash(trace):
            raise AssertionError("Strategies did not replay identical trace")
        for event in result["events"]:
            if "state_after" in event:
                s = event["state_after"]
                if not (0 <= s["remaining_memory_mib"] <= s["capacity_memory_mib"] and
                        0 <= s["remaining_compute_units"] <= 100 and 0 <= s["allocated_instances"] <= 10):
                    raise AssertionError("Over-allocation in regression")
        if not all(not s.allocations and s.remaining_memory_mib == s.profile.modeled_memory_mib and s.remaining_compute_units == 100 for s in fleet.values()):
            raise AssertionError("Resource release regression")
        summaries.append(result["metrics"])
        audit[name] = {"completed": result["metrics"]["completed_total_after_drain"],
                       "no_overallocation": True, "all_resources_restored": True,
                       "resource_state_changes": sum(e["event"] in ("start", "finish") for e in result["events"]),
                       "dynamic_repredictions": sum(e.get("state_prediction_after") is not None for e in result["events"])}
        results[name] = result
    return {"summary": summaries, "audit": audit, "results": results,
            "fleet": {k: sum(s.profile.profile_id == k for s in create_fleet(profiles).values()) for k in profiles},
            "scope": "compatibility smoke, NOT a comparative scheduling efficacy study; NVIDIA-only independent legacy synthetic execution oracle"}
