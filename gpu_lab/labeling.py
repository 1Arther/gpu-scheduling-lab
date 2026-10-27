"""Reference-label oracle ONLY. Never import this as a simulation duration oracle.

    t = max(work / (peak_fp32 * eta_c * q), traffic / (bandwidth * eta_b * q))
Here q is unreserved compute/100. Proportional bandwidth is an explicit synthetic
assumption, not measured vGPU isolation. Counts/types do not add invented penalties.
"""
from dataclasses import dataclass
import json
import math
from pathlib import Path

from .profiles import ROOT
from .resources import GPUState, ResourceRequest

LABELS = ("training_preferred", "inference_preferred", "balanced", "unavailable")
LABEL_NAMES_ZH = dict(zip(LABELS, ("训练优先", "推理优先", "训推一体", "暂不可用")))
DEFAULT_CONTRACT = ROOT / "data/reference_workloads.json"


@dataclass(frozen=True)
class ReferenceOracle:
    contract: dict

    @classmethod
    def load(cls, path: Path = DEFAULT_CONTRACT):
        contract = json.loads(Path(path).read_text(encoding="utf-8"))
        a = contract["assumptions"]
        for key in ("compute_efficiency", "bandwidth_efficiency"):
            if not 0 < a[key] <= 1:
                raise ValueError(f"Invalid efficiency: {key}")
        if {w["task_type"] for w in contract["workloads"]} != {"training", "inference"}:
            raise ValueError("Need one training and one inference contract")
        for w in contract["workloads"]:
            ResourceRequest(w["memory_mib"], w["min_compute_units"])
            for key in ("work_tflop", "memory_traffic_gb", "max_latency_s", "min_throughput_per_s"):
                if not math.isfinite(w[key]) or w[key] <= 0:
                    raise ValueError(f"Invalid reference workload: {key}")
        return cls(contract)

    def evaluate(self, state: GPUState) -> dict:
        a = self.contract["assumptions"]
        q = state.remaining_compute_units / 100
        details = {}
        for w in self.contract["workloads"]:
            time_budget = min(w["max_latency_s"], 1 / w["min_throughput_per_s"])
            required_compute = max(w["min_compute_units"], math.ceil(100 * max(
                w["work_tflop"] / (state.profile.fp32_tflops * a["compute_efficiency"] * time_budget),
                w["memory_traffic_gb"] / (state.profile.bandwidth_gb_s * a["bandwidth_efficiency"] * time_budget))))
            hard_ok = state.can_allocate(ResourceRequest(w["memory_mib"], required_compute))
            if q <= 0:
                latency = None
                throughput = 0.0
            else:
                compute_s = w["work_tflop"] / (state.profile.fp32_tflops * a["compute_efficiency"] * q)
                memory_s = w["memory_traffic_gb"] / (state.profile.bandwidth_gb_s * a["bandwidth_efficiency"] * q)
                latency = max(compute_s, memory_s)
                throughput = 1 / latency
            meets = (hard_ok and latency is not None and latency <= w["max_latency_s"]
                     and throughput >= w["min_throughput_per_s"])
            details[w["task_type"]] = {
                "hard_resources_ok": hard_ok,
                "required_compute_units_for_reference_slo": required_compute,
                "latency_budget_compute_units": state.remaining_compute_units,
                "analytical_latency_s": latency,
                "analytical_throughput_per_s": throughput,
                "meets_reference": meets,
            }
        train = details["training"]["meets_reference"]
        infer = details["inference"]["meets_reference"]
        label = ("balanced" if train and infer else "training_preferred" if train
                 else "inference_preferred" if infer else "unavailable")
        return {"label": label, "label_kind": "synthetic_analytical_reference", "reference_details": details}
