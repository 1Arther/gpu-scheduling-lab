"""Deterministic synthetic supervisor; never consumes RF predictions or marketing."""
import math
from .catalog import REQUIRED
from .features import ACCEPTED_QUOTAS, validate_state

LABELS = ("training_preferred", "inference_preferred", "balanced", "unavailable")
UNKNOWN = "pending_verification"
NAMES = {"training_preferred": "训练优先型", "inference_preferred": "推理优先型", "balanced": "训推一体型",
         "unavailable": "当前不满足参考负载要求", UNKNOWN: "信息不足、待验证"}
CONTRACT = {
    "contract_id": "cross-gpu-fp32-synthetic-v3", "label_source": "synthetic",
    "precision": "dense IEEE FP32 general/vector, NOT TF32 or matrix-FP32 peak",
    "compute_efficiency_assumption": .35, "bandwidth_efficiency_assumption": .55,
    "bandwidth_sharing_assumption": "proportional to an EXPLICIT hypothetical device share; not native cross-vendor quota equivalence",
    "training": {"memory_mib": 8192, "min_compute_quota": 20, "work_tflop": 2., "traffic_gb": 8., "max_latency_s": .2, "min_throughput_s": 5.},
    "inference": {"memory_mib": 4096, "min_compute_quota": 10, "work_tflop": .06, "traffic_gb": 2.5, "max_latency_s": .01, "min_throughput_s": 100.},
    "limitation": "Reference contracts and efficiencies are declared assumptions, not named-model benchmarks. Low-precision fields are not used to generate FP32 labels."
}


def combine(training, inference):
    if training is None or inference is None:
        return UNKNOWN
    return "balanced" if training and inference else "training_preferred" if training else "inference_preferred" if inference else "unavailable"


def reasons_unusable(device, state):
    reasons = []
    for k in REQUIRED:
        value = device.values(comparable=True)[k]
        if value is None:
            reasons.append(f"missing_required:{k}")
        elif isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            reasons.append(f"invalid_required:{k}")
    if device.record["device_type"] != "GPU":
        reasons.append("device_type_not_in_unified_GPU_model")
    if state.get("quota_semantics") not in ACCEPTED_QUOTAS:
        reasons.append("unmapped_backend_quota_semantics")
    reasons += validate_state(device, state)
    return reasons


def hard_checks(device, state):
    if validate_state(device, state):
        return {"training": False, "inference": False}
    return {kind: (state.get("free_memory_mib") is not None and state.get("free_compute_quota") is not None
                   and state["free_memory_mib"] >= w["memory_mib"]
                   and state["free_compute_quota"] >= w["min_compute_quota"]
                   and state.get("allocated_vgpu_count", 10) < state.get("instance_limit", 10))
            for kind, w in ((k, CONTRACT[k]) for k in ("training", "inference"))}


def assess(device, state, conservative_factor=1.):
    reasons = reasons_unusable(device, state)
    if reasons:
        return {"label_source": "unknown", "training": None, "inference": None, "label": UNKNOWN,
                "reasons": reasons, "hard_constraints": hard_checks(device, state)}
    values, q = device.values(comparable=True), state["free_compute_quota"] / 100
    details, hard = {}, hard_checks(device, state)
    for kind in ("training", "inference"):
        w = CONTRACT[kind]
        t = max(w["work_tflop"] / (values["fp32_tflops"] * .35 * q * conservative_factor),
                w["traffic_gb"] / (values["memory_bandwidth_gbps"] * .55 * q * conservative_factor)) if q > 0 else None
        ok = hard[kind] and t is not None and t <= min(w["max_latency_s"], 1 / w["min_throughput_s"])
        details[kind] = {"adapted": bool(ok), "theoretical_latency_s": t, "resource_minimum_met": hard[kind],
                         "theoretical_suitability_score": min(1., w["max_latency_s"] / t) if hard[kind] and t else 0.}
    train, infer = details["training"]["adapted"], details["inference"]["adapted"]
    return {"label_source": "synthetic", "training": train, "inference": infer, "label": combine(train, infer),
            "details": details, "conservative_factor": conservative_factor,
            "note": "Theoretical assumption only, not independently measured performance"}
