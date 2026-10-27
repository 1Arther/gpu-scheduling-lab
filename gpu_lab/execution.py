"""Synthetic execution oracle. No classifier, labeling, scoring or reference SLO imports.

Rate is work-progress per second. Reference work is solo L40/full-quota seconds.
Co-run rates are recomputed whenever arrivals/completions change the active set.
The scheduler receives NO access to this object or per-Pod execution work/pressure.
"""
import hashlib
import json
import math

from .profiles import ROOT

DEFAULT_EXECUTION = ROOT / "data/synthetic/execution-model.json"


class ExecutionModel:
    def __init__(self, seed=707, interference=1.0, path=DEFAULT_EXECUTION):
        if not math.isfinite(interference) or interference < 0:
            raise ValueError("interference must be finite and nonnegative")
        self.config = json.loads(path.read_text(encoding="utf-8"))
        self.seed = seed
        self.interference = interference

    def jitter(self, pod_id, profile_id):
        digest = hashlib.sha256(f"{self.seed}|{pod_id}|{profile_id}".encode()).digest()
        uniform = int.from_bytes(digest[:8], "big") / (2**64 - 1)
        lo, hi = self.config["jitter_range"]
        return lo + (hi - lo) * uniform

    def rates(self, state, pods):
        if not pods:
            return {}
        profile = state.profile
        ref = self.config["reference"]
        total_quota = sum(p.request.compute_units for p in pods) / 100
        result = {}
        for pod in pods:
            params = self.config[pod.task_type]
            weight = params["compute_weight"]
            hardware = 1 / (weight * ref["fp32_tflops"] / profile.fp32_tflops
                            + (1 - weight) * ref["bandwidth_gb_s"] / profile.bandwidth_gb_s)
            quota = pod.request.compute_units / 100
            scaling = 1 / (params["serial_fraction"]
                           + (1 - params["serial_fraction"]) / quota ** params["quota_exponent"])
            others = [p for p in pods if p.pod_id != pod.pod_id]
            opposite = sum(p.task_type != pod.task_type for p in others)
            pressure = sum(p.pressure for p in others)
            penalty = 1 + self.interference * total_quota ** 1.2 * (
                params["per_neighbor"] * len(others) + params["per_opposite_type"] * opposite
                + params["per_neighbor_pressure"] * pressure)
            rate = hardware * scaling * self.jitter(pod.pod_id, profile.profile_id) / penalty
            if not math.isfinite(rate) or rate <= 0:
                raise ValueError("Invalid synthetic progress rate")
            result[pod.pod_id] = rate
        return result
