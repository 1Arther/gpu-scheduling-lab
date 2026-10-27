"""Repredict after every committed allocation/release, subject to hard resources."""
from .resources import GPUState, Allocation


class DynamicGPU:
    def __init__(self, state: GPUState, predictor):
        self.state = state
        self.predictor = predictor
        self.history = []
        self._record("initial")

    def _record(self, event):
        row = {"event": event, "state": self.state.snapshot(), "prediction": self.predictor.predict(self.state)}
        self.history.append(row)
        return row

    def allocate(self, allocation: Allocation):
        self.state.allocate(allocation)  # Prediction never overrides hard resource constraints.
        return self._record(f"allocate:{allocation.allocation_id}")

    def release(self, allocation_id):
        self.state.release(allocation_id)
        return self._record(f"release:{allocation_id}")
