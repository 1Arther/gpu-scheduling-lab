"""Numeric-only inputs; identities, vendors and architecture strings never enter RF."""
import hashlib
import json
import math

from ..resources import GPUState
from .catalog import STATIC

DYNAMIC = ("free_memory_mib", "free_compute_quota", "allocated_vgpu_count", "running_training_count", "running_inference_count")
DERIVED = ("free_memory_ratio", "free_compute_quota_ratio")
FEATURES = STATIC + DYNAMIC + DERIVED
SYNTHETIC_QUOTA = "synthetic_device_share_100_v1"
ACCEPTED_QUOTAS = {SYNTHETIC_QUOTA, "physical_unpartitioned"}


def initial_state(device, semantics="physical_unpartitioned"):
    return {"free_memory_mib": device.values()["memory_capacity_mib"], "free_compute_quota": 100,
            "allocated_vgpu_count": 0, "running_training_count": 0, "running_inference_count": 0,
            "quota_semantics": semantics, "instance_limit": 10,
            "data_kind": "synthetic_initial_resources_not_live_measurement"}


def from_ledger(ledger: GPUState):
    return {"free_memory_mib": ledger.remaining_memory_mib, "free_compute_quota": ledger.remaining_compute_units,
            "allocated_vgpu_count": ledger.allocated_instances,
            "running_training_count": ledger.instances_of_type("training"),
            "running_inference_count": ledger.instances_of_type("inference"), "instance_limit": ledger.max_instances,
            "quota_semantics": SYNTHETIC_QUOTA, "data_kind": "synthetic_resource_state"}


def features(device, state):
    values = device.values(comparable=True)
    cap, free, quota = values["memory_capacity_mib"], state.get("free_memory_mib"), state.get("free_compute_quota")
    numeric = lambda v: not isinstance(v, bool) and isinstance(v, (int, float)) and math.isfinite(v)
    return {**values, **{name: state.get(name) for name in DYNAMIC},
            "free_memory_ratio": free / cap if numeric(cap) and cap > 0 and numeric(free) else None,
            "free_compute_quota_ratio": quota / 100 if numeric(quota) else None}


def validate_state(device, state):
    reasons = []
    values = device.values()
    cap = values["memory_capacity_mib"]
    for name in DYNAMIC:
        value = state.get(name)
        if value is None or isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            reasons.append(f"missing_or_invalid_dynamic:{name}")
    if reasons:
        return reasons
    if cap is not None and state["free_memory_mib"] > cap:
        reasons.append("free_memory_exceeds_physical_capacity")
    if state["free_compute_quota"] > 100:
        reasons.append("compute_quota_exceeds_declared_100_units")
    n, limit = state["allocated_vgpu_count"], state.get("instance_limit", 10)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        reasons.append("invalid_instance_limit")
        limit = 10
    if n != int(n) or n > limit:
        reasons.append("invalid_instance_count")
    if any(state[k] != int(state[k]) for k in ("running_training_count", "running_inference_count")):
        reasons.append("invalid_task_count")
    if state["running_training_count"] + state["running_inference_count"] != n:
        reasons.append("existing_task_counts_do_not_match_instances")
    if state.get("quota_semantics") == "physical_unpartitioned" and (n != 0 or state["free_compute_quota"] != 100):
        reasons.append("physical_unpartitioned_cannot_describe_shared_state")
    return reasons


def feature_hash(row):
    return hashlib.sha256(json.dumps(row, sort_keys=True, allow_nan=False).encode()).hexdigest()
