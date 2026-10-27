"""Pod-aware hybrid ranking: hard feasibility, RF soft hints, explicit heuristics.

This is not an independently trained performance regressor. The only learned
part is the RF class probabilities. No duration-oracle access or execution work.
"""
from dataclasses import dataclass
import json

from .features import state_features
from .profiles import ROOT
from .resources import ResourceRequest

DEFAULT_SCORING = ROOT / "data/synthetic/scoring-policy.json"


@dataclass(frozen=True)
class PodRequest:
    """Only information visible to a scheduler, excluding hidden simulated work."""
    task_type: str
    request: ResourceRequest

    @classmethod
    def from_pod(cls, pod):
        return cls(pod.task_type, pod.request)


class PredictionCache:
    def __init__(self, model):
        self.model = model
        self.cache = {}

    def key(self, state):
        custom_key = getattr(self.model, "cache_key", None)
        if callable(custom_key):
            return custom_key(state)
        features = state_features(state)
        return (state.profile.profile_id, *(features[n] for n in self.model.metadata["feature_names"]))

    def predict_many(self, states):
        missing = {}
        for state in states:
            key = self.key(state)
            if key not in self.cache:
                missing[key] = state
        if missing:
            predictions = self.model.predict_many(list(missing.values()))
            self.cache.update(zip(missing, predictions))
        return [self.cache[self.key(s)] for s in states]


def packing_residual(state, request):
    return (0.5 * (state.remaining_memory_mib - request.memory_mib) / state.profile.modeled_memory_mib
            + 0.4 * (state.remaining_compute_units - request.compute_units) / 100
            + 0.1 * (state.remaining_instances - request.instances) / state.max_instances)


def task_probability(prediction, task_type):
    probabilities = prediction["class_probabilities"]
    return probabilities[task_type + "_preferred"] + probabilities["balanced"]


class PodScorer:
    def __init__(self, model, profiles, path=DEFAULT_SCORING):
        self.predictions = PredictionCache(model)
        self.config = json.loads(path.read_text(encoding="utf-8"))
        if abs(sum(self.config["weights"].values()) - 1) > 1e-9:
            raise ValueError("Scoring weights must sum to one")
        self.max_fp32 = max(p.fp32_tflops for p in profiles.values())
        self.max_bandwidth = max(p.bandwidth_gb_s for p in profiles.values())

    def score_many(self, states, pod: PodRequest):
        if pod.task_type not in ("training", "inference"):
            raise ValueError("Explicit task type required")
        feasible = [s for s in states if s.can_allocate(pod.request)]
        predictions = dict(zip((s.gpu_id for s in feasible), self.predictions.predict_many(feasible)))
        scores = []
        for state in states:
            inputs = {**state_features(state), "pod_type": pod.task_type,
                      "pod_memory_mib": pod.request.memory_mib, "pod_compute_units": pod.request.compute_units,
                      "pod_instances": pod.request.instances}
            if state.gpu_id not in predictions:
                scores.append({"gpu_id": state.gpu_id, "feasible": False, "score": None,
                               "reason": "physical memory/compute/instance constraint", "inputs": inputs})
                continue
            prediction = predictions[state.gpu_id]
            params = self.config[pod.task_type]
            opposite_type = "inference" if pod.task_type == "training" else "training"
            other = state.instances_of_type(opposite_type)
            hardware = (params["compute_weight"] * state.profile.fp32_tflops / self.max_fp32
                        + (1 - params["compute_weight"]) * state.profile.bandwidth_gb_s / self.max_bandwidth)
            hardware *= 0.5 + 0.5 * pod.request.compute_units / 100
            hardware /= 1 + params["per_neighbor"] * state.allocated_instances + params["per_opposite_type"] * other
            post_mem = (state.remaining_memory_mib - pod.request.memory_mib) / state.profile.modeled_memory_mib
            post_compute = (state.remaining_compute_units - pod.request.compute_units) / 100
            components = {"model_probability": task_probability(prediction, pod.task_type),
                          "hardware_sharing": hardware, "packing": 1 - packing_residual(state, pod.request),
                          "balance": 1 - abs(post_mem - post_compute)}
            score = sum(self.config["weights"][k] * v for k, v in components.items())
            scores.append({"gpu_id": state.gpu_id, "feasible": True, "score": score,
                           "components": components, "prediction": prediction, "inputs": inputs})
        slack = self.config.get("packing_slack")
        guard_enabled = pod.task_type in self.config.get("guard_task_types", ["training", "inference"])
        if feasible and self.config.get("guard_memory_dominated_only"):
            smallest = min(s.profile.modeled_memory_mib for s in feasible)
            guard_enabled = guard_enabled and pod.request.memory_mib / smallest > pod.request.compute_units / 100
        if slack is not None and feasible and guard_enabled:
            best = min(packing_residual(s, pod.request) for s in feasible)
            by_id = {s.gpu_id: s for s in feasible}
            for row in scores:
                if row["feasible"]:
                    residual = packing_residual(by_id[row["gpu_id"]], pod.request)
                    near_best_fit = residual <= best + slack + 1e-12
                    row["raw_hybrid_score"] = row["score"]
                    row["near_best_fit"] = near_best_fit
                    row["packing_residual"] = residual
                    row["score"] += float(near_best_fit)
        return scores

    def score(self, state, pod: PodRequest):
        return self.score_many([state], pod)[0]
