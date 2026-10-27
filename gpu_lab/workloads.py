"""Immutable finite Pod traces, generated once and replayed by every strategy."""
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import random

from .resources import ResourceRequest, TASK_TYPES, positive_integer

SCENARIOS = ("steady_mixed", "bursty_mixed", "training_heavy", "memory_heavy")


@dataclass(frozen=True)
class Pod:
    pod_id: str
    arrival_s: float
    task_type: str
    request: ResourceRequest
    reference_work_s: float
    requests: int = 1
    pressure: float = 1.0
    response_slo_s: float | None = None

    def __post_init__(self):
        if not self.pod_id or self.task_type not in TASK_TYPES:
            raise ValueError("Pod needs a unique ID and explicit training/inference type")
        if not math.isfinite(self.arrival_s) or self.arrival_s < 0:
            raise ValueError("arrival must be finite and >=0")
        if not math.isfinite(self.reference_work_s) or self.reference_work_s <= 0:
            raise ValueError("reference_work_s must be positive")
        if not math.isfinite(self.pressure) or self.pressure <= 0:
            raise ValueError("pressure must be positive")
        positive_integer(self.requests, "requests")
        if self.response_slo_s is not None and (not math.isfinite(self.response_slo_s) or self.response_slo_s <= 0):
            raise ValueError("response SLO must be positive")

    def record(self):
        return {"data_kind": "synthetic_pod_trace", **asdict(self)}


def trace_hash(pods):
    return hashlib.sha256(json.dumps([p.record() for p in pods], sort_keys=True).encode()).hexdigest()


def generate_trace(scenario, seed=11, count=120):
    if scenario not in SCENARIOS or count <= 0:
        raise ValueError("Unknown scenario or invalid Pod count")
    # Stable across processes; never use Python's randomized hash().
    stable = int.from_bytes(hashlib.sha256(scenario.encode()).digest()[:8], "big")
    rng = random.Random(seed + stable)
    t, pods = 0.0, []
    for i in range(count):
        if scenario == "bursty_mixed":
            t = (i // 15) * 42 + (i % 15) * 0.08 + rng.random() * 0.04
        else:
            t += rng.expovariate(1 / 3.5)
        training_fraction = 0.75 if scenario == "training_heavy" else 0.45
        task_type = "training" if rng.random() < training_fraction else "inference"
        if task_type == "training":
            memory = rng.choice((6144, 8192, 12288, 16384, 20480))
            compute = rng.choice((20, 30, 40, 50, 60))
            work = rng.uniform(30, 110)
            requests, slo = 1, None
            if scenario == "memory_heavy":
                memory = rng.choice((16384, 20480, 28672, 36864))
                compute = rng.choice((10, 20, 30))
        else:
            memory = rng.choice((2048, 4096, 8192, 12288))
            compute = rng.choice((10, 15, 25, 35))
            requests = rng.choice((8, 16, 32))
            work = rng.uniform(0.02, 0.08) * requests
            slo = 1.0 + requests * 0.15  # Synthetic batch response budget, unrelated to label SLO.
            if scenario == "memory_heavy":
                memory = rng.choice((8192, 12288, 16384))
        pods.append(Pod(f"{scenario}-{seed}-{i:04d}", t, task_type, ResourceRequest(memory, compute),
                        work, requests, rng.uniform(0.6, 1.5), slo))
    return sorted(pods, key=lambda p: (p.arrival_s, p.pod_id))
