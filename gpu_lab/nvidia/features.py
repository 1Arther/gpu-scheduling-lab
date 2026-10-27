"""Feature engineering uses available inputs only, with no reference thresholds."""
from ..crossgpu.features import FEATURES, features as base_features

EFFECTIVE = ("effective_fp32_tflops", "effective_bandwidth_gbps")
COMPUTE_BANDWIDTH = "compute_bandwidth_flop_per_byte"
FEATURE_SETS = {
    "baseline14": FEATURES,
    "absolute_only": tuple(k for k in FEATURES if k not in ("free_memory_ratio","free_compute_quota_ratio")),
    "relative_memory": tuple(k for k in FEATURES if k not in ("memory_capacity_mib","free_memory_mib")),
    "no_static_capacity": tuple(k for k in FEATURES if k != "memory_capacity_mib"),
    "effective_compute": FEATURES + EFFECTIVE[:1],
    "effective_bandwidth": FEATURES + EFFECTIVE[1:],
    "effective_both": FEATURES + EFFECTIVE,
    "effective_both_balance": FEATURES + EFFECTIVE + (COMPUTE_BANDWIDTH,),
}


def enrich(row):
    row = dict(row)
    q, fp32, bw = row.get("free_compute_quota_ratio"), row.get("fp32_tflops"), row.get("memory_bandwidth_gbps")
    row["effective_fp32_tflops"] = fp32*q if fp32 is not None and q is not None else None
    row["effective_bandwidth_gbps"] = bw*q if bw is not None and q is not None else None
    row[COMPUTE_BANDWIDTH] = fp32*1000/bw if fp32 is not None and bw else None
    return row


def features(device, state):
    return enrich(base_features(device, state))
