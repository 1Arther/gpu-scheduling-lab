"""Lightweight event simulation with changing co-run rates and exact reservations."""
from dataclasses import dataclass
import math

import numpy as np

from .fleet import create_fleet
from .resources import Allocation
from .scoring import PodRequest
from .workloads import trace_hash

EPS = 1e-9


@dataclass
class Running:
    pod: object
    gpu_id: str
    start_s: float
    remaining_work_s: float


def mean(values):
    return float(np.mean(values)) if values else None


def percentile(values, quantile):
    return float(np.percentile(values, quantile)) if values else None


def simulate(pods, profiles, scheduler, execution, horizon_s=600.0, fleet=None):
    if not math.isfinite(horizon_s) or horizon_s <= 0:
        raise ValueError("Positive finite observation horizon required")
    ordered = sorted(pods, key=lambda p: (p.arrival_s, p.pod_id))
    if len({p.pod_id for p in ordered}) != len(ordered):
        raise ValueError("Duplicate Pod ID")
    if ordered and ordered[-1].arrival_s > horizon_s:
        raise ValueError("All trace arrivals must be inside the common observation horizon")
    fleet = create_fleet(profiles) if fleet is None else fleet
    if not fleet or any(state.allocations for state in fleet.values()):
        raise ValueError("Simulation needs a nonempty fresh fleet")
    active, records, waiting, events, timeline, valid = {}, [], [], [], [], []
    for pod in ordered:
        if any(state.can_allocate(pod.request) for state in fleet.values()):
            valid.append(pod)
        else:
            records.append({"pod_id": pod.pod_id, "task_type": pod.task_type, "status": "rejected",
                            "arrival_s": pod.arrival_s, "reason": "request exceeds every empty physical GPU"})
    total_memory = sum(s.profile.modeled_memory_mib for s in fleet.values())
    total_compute = len(fleet) * 100
    area = {"compute": 0.0, "memory": 0.0, "active": 0.0}
    next_arrival, t, max_waiting = 0, 0.0, 0
    while next_arrival < len(valid) or active or waiting:
        while next_arrival < len(valid) and valid[next_arrival].arrival_s <= t:
            pod = valid[next_arrival]
            waiting.append(pod)
            events.append({"time_s": t, "event": "arrival", "pod_id": pod.pod_id})
            next_arrival += 1
        max_waiting = max(max_waiting, len(waiting))
        keep_waiting = []
        # FCFS feasible backfill, same for every strategy; no priority or preemption.
        for pod in waiting:
            state, ranking = scheduler.choose(fleet, PodRequest.from_pod(pod))
            if state is None:
                keep_waiting.append(pod)
                continue
            state.allocate(Allocation(pod.pod_id, pod.task_type, pod.request))
            active[pod.pod_id] = Running(pod, state.gpu_id, t, pod.reference_work_s)
            events.append({"time_s": t, "event": "start", "pod_id": pod.pod_id,
                           "gpu_id": state.gpu_id, "state_after": state.snapshot(), "ranking": ranking,
                           "state_prediction_after": scheduler.on_state_change(state)})
        waiting = keep_waiting
        rates = {}
        for gpu_id, state in fleet.items():
            colocated = [r.pod for r in active.values() if r.gpu_id == gpu_id]
            rates.update(execution.rates(state, colocated))
        arrival_time = valid[next_arrival].arrival_s if next_arrival < len(valid) else math.inf
        finish_time = min((t + r.remaining_work_s / rates[pod_id] for pod_id, r in active.items()), default=math.inf)
        next_t = min(arrival_time, finish_time)
        if not math.isfinite(next_t):
            if waiting:
                raise RuntimeError("No progress: feasible pending jobs with no active or future event")
            break
        dt = max(0.0, next_t - t)
        compute = sum(100 - s.remaining_compute_units for s in fleet.values()) / total_compute
        memory = sum(s.profile.modeled_memory_mib - s.remaining_memory_mib for s in fleet.values()) / total_memory
        occupied = sum(bool(s.allocations) for s in fleet.values()) / len(fleet)
        overlap = max(0.0, min(next_t, horizon_s) - min(t, horizon_s))
        for name, fraction in (("compute", compute), ("memory", memory), ("active", occupied)):
            area[name] += overlap * fraction
        if t < horizon_s and next_t > t:
            timeline.append({"start_s": t, "end_s": min(next_t, horizon_s),
                             "reserved_compute_fraction": compute, "reserved_memory_fraction": memory,
                             "active_gpu_fraction": occupied, "queued_pods": len(waiting), "running_pods": len(active)})
        for pod_id, running in active.items():
            running.remaining_work_s -= rates[pod_id] * dt
        t = next_t
        # Completions at the same timestamp are all released before admitting arrivals.
        completed = sorted(pod_id for pod_id, r in active.items()
                           if r.remaining_work_s <= max(EPS, r.pod.reference_work_s * 1e-10))
        for pod_id in completed:
            running = active.pop(pod_id)
            pod, state = running.pod, fleet[running.gpu_id]
            state.release(pod_id)
            records.append({"pod_id": pod_id, "task_type": pod.task_type, "status": "completed",
                            "gpu_id": state.gpu_id, "profile_id": state.profile.profile_id,
                            "arrival_s": pod.arrival_s, "start_s": running.start_s, "finish_s": t,
                            "wait_s": running.start_s - pod.arrival_s, "runtime_s": t - running.start_s,
                            "jct_s": t - pod.arrival_s, "requests": pod.requests,
                            "response_slo_s": pod.response_slo_s,
                            "memory_mib": pod.request.memory_mib, "compute_units": pod.request.compute_units})
            events.append({"time_s": t, "event": "finish", "pod_id": pod_id, "gpu_id": state.gpu_id,
                           "state_after": state.snapshot(), "state_prediction_after": scheduler.on_state_change(state)})
    assert all(not state.allocations and state.remaining_compute_units == 100 for state in fleet.values())
    done = [r for r in records if r["status"] == "completed"]
    train = [r for r in done if r["task_type"] == "training"]
    infer = [r for r in done if r["task_type"] == "inference"]
    in_window = [r for r in done if r["finish_s"] <= horizon_s + EPS]
    requests = sum(r["requests"] for r in infer)
    metrics = {
        "strategy": scheduler.name, "trace_sha256": trace_hash(ordered),
        "execution_seed": execution.seed, "interference": execution.interference,
        "observation_horizon_s": horizon_s, "submitted_pods": len(ordered),
        "completed_total_after_drain": len(done), "completed_within_horizon": len(in_window),
        "unfinished_at_horizon": len(valid) - len(in_window),
        "rejected_pods": len(ordered) - len(valid),
        "mean_wait_s": mean([r["wait_s"] for r in done]),
        "wait_p95_s": percentile([r["wait_s"] for r in done], 95),
        "training_pods": len(train), "training_mean_wait_s": mean([r["wait_s"] for r in train]),
        "training_mean_jct_s": mean([r["jct_s"] for r in train]),
        "training_mean_runtime_s": mean([r["runtime_s"] for r in train]),
        "inference_pods": len(infer), "inference_mean_wait_s": mean([r["wait_s"] for r in infer]),
        "inference_pod_response_p95_s": percentile([r["jct_s"] for r in infer], 95),
        "inference_pod_mean_response_s": mean([r["jct_s"] for r in infer]),
        "inference_mean_serial_service_per_request_s": sum(r["runtime_s"] for r in infer) / requests if requests else None,
        "inference_batch_slo_met_fraction": mean([float(r["jct_s"] <= r["response_slo_s"]) for r in infer if r["response_slo_s"] is not None]),
        "inference_completed_batch_requests_per_s_within_horizon": sum(r["requests"] for r in in_window if r["task_type"] == "inference") / horizon_s,
        "reserved_compute_utilization": area["compute"] / horizon_s,
        "reserved_memory_utilization": area["memory"] / horizon_s,
        "active_gpu_utilization": area["active"] / horizon_s,
        "drain_makespan_s": t, "max_queued_pods": max_waiting,
        "data_kind": "synthetic_discrete_event_simulation_not_real_measurements",
    }
    return {"metrics": metrics, "pods": sorted(records, key=lambda r: r["pod_id"]),
            "events": events, "timeline": timeline}
