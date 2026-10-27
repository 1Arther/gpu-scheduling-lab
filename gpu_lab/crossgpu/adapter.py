"""Use the new numeric RF with the existing NVIDIA-only offline simulator."""
from .features import FEATURES, features, from_ledger, initial_state
from .theory import LABELS, UNKNOWN


class LegacyAdapter:
    def __init__(self, model, devices, static=False):
        self.model, self.devices, self.static = model, devices, static
        self.metadata = {"feature_names": list(FEATURES), "adapter": "round3", "static": static}

    def inputs(self, ledger):
        device = self.devices[ledger.profile.profile_id]
        return device, initial_state(device) if self.static else from_ledger(ledger)

    def cache_key(self, ledger):
        device, state = self.inputs(ledger)
        return (device.device_id, state["quota_semantics"], state["instance_limit"], *features(device, state).values())

    def predict_many(self, ledgers):
        predictions = []
        for ledger in ledgers:
            device, state = self.inputs(ledger)
            if device.record["vendor"] != "NVIDIA" or device.record["software_backend"] != "CUDA":
                raise ValueError("The legacy execution simulator is only validated as a NVIDIA synthetic model")
            prediction = self.model.predict_device(device, state)
            probabilities = dict.fromkeys(LABELS, 0.)
            if prediction["label"] != UNKNOWN:
                raw = prediction["raw_rf"]
                t = raw["training_vote_fraction"] if prediction["hard_constraints"]["training"] else 0.
                i = raw["inference_vote_fraction"] if prediction["hard_constraints"]["inference"] else 0.
                # Product of marginals is an interface approximation, NOT a learned joint posterior.
                probabilities.update(training_preferred=t*(1-i), inference_preferred=(1-t)*i,
                                     balanced=t*i, unavailable=(1-t)*(1-i))
            else:
                probabilities[UNKNOWN] = 1.
            predictions.append({**prediction, "class_probabilities": probabilities,
                                "class_probability_basis": "product of two RF marginals for legacy interface; not calibrated joint reliability"})
        return predictions

    def predict(self, ledger):
        return self.predict_many([ledger])[0]
