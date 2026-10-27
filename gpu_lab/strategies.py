"""All strategies share feasibility, queue order, placements and release semantics."""
from .scoring import PodRequest, PodScorer, PredictionCache, packing_residual, task_probability

STRATEGIES = ("binpack", "static_rf", "dynamic_rf")


class Scheduler:
    def __init__(self, name, profiles, static_model=None, dynamic_model=None, scoring_path=None):
        if name not in STRATEGIES:
            raise ValueError("Unknown strategy")
        self.name = name
        self.static_predictions = PredictionCache(static_model) if static_model is not None else None
        kwargs = {"path": scoring_path} if scoring_path else {}
        self.scorer = PodScorer(dynamic_model, profiles, **kwargs) if dynamic_model is not None else None

    def on_state_change(self, state):
        if self.name == "dynamic_rf":
            return self.scorer.predictions.predict_many([state])[0]
        if self.name == "static_rf":
            return self.static_predictions.predict_many([state])[0]
        return None

    def choose(self, fleet, pod_request: PodRequest):
        feasible = [s for s in fleet.values() if s.can_allocate(pod_request.request)]
        if not feasible:
            return None, None
        if self.name == "binpack":
            state = min(feasible, key=lambda s: (packing_residual(s, pod_request.request), s.gpu_id))
            return state, {"policy": self.name, "packing_residual": packing_residual(state, pod_request.request)}
        if self.name == "static_rf":
            predictions = self.static_predictions.predict_many(feasible)
            choices = list(zip(feasible, predictions))
            state, prediction = min(choices, key=lambda pair: (
                -task_probability(pair[1], pod_request.task_type),
                packing_residual(pair[0], pod_request.request), pair[0].gpu_id))
            return state, {"policy": self.name, "prediction": prediction,
                           "task_probability": task_probability(prediction, pod_request.task_type)}
        scores = self.scorer.score_many(feasible, pod_request)
        selected = min(scores, key=lambda row: (-row["score"], row["gpu_id"]))
        return fleet[selected["gpu_id"]], {"policy": self.name, **selected}
