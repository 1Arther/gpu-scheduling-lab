"""Integer reservation ledger; compute units are quota, not observed utilization."""
from dataclasses import asdict, dataclass
from types import MappingProxyType

from .profiles import GPUProfile

TASK_TYPES = ("training", "inference")


def positive_integer(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True)
class ResourceRequest:
    memory_mib: int
    compute_units: int
    instances: int = 1

    def __post_init__(self):
        for name in ("memory_mib", "compute_units", "instances"):
            positive_integer(getattr(self, name), name)


@dataclass(frozen=True)
class Allocation:
    allocation_id: str
    task_type: str
    request: ResourceRequest

    def __post_init__(self):
        if not isinstance(self.allocation_id, str) or not self.allocation_id.strip():
            raise ValueError("allocation_id must be nonempty")
        if self.task_type not in TASK_TYPES:
            raise ValueError(f"task_type must be one of {TASK_TYPES}")


class GPUState:
    """One physical GPU; every request is an aggregate reservation on that GPU.

    Multiple-vGPU placement across physical GPUs will need a Pod placement layer.
    No request is accepted here unless all three local constraints hold.
    """

    def __init__(self, gpu_id: str, profile: GPUProfile, max_instances: int = 10):
        positive_integer(max_instances, "max_instances")
        self.gpu_id = gpu_id
        self.profile = profile
        self.max_instances = max_instances
        self._allocations: dict[str, Allocation] = {}
        self.revision = 0

    @property
    def allocations(self):
        return MappingProxyType(self._allocations)

    @property
    def remaining_memory_mib(self):
        return self.profile.modeled_memory_mib - sum(a.request.memory_mib for a in self._allocations.values())

    @property
    def remaining_compute_units(self):
        return 100 - sum(a.request.compute_units for a in self._allocations.values())

    @property
    def allocated_instances(self):
        return sum(a.request.instances for a in self._allocations.values())

    @property
    def remaining_instances(self):
        return self.max_instances - self.allocated_instances

    def instances_of_type(self, task_type):
        return sum(a.request.instances for a in self._allocations.values() if a.task_type == task_type)

    def can_allocate(self, request: ResourceRequest) -> bool:
        return (request.memory_mib <= self.remaining_memory_mib
                and request.compute_units <= self.remaining_compute_units
                and request.instances <= self.remaining_instances)

    def allocate(self, allocation: Allocation):
        if allocation.allocation_id in self._allocations:
            raise ValueError(f"Duplicate allocation: {allocation.allocation_id}")
        if not self.can_allocate(allocation.request):
            raise ValueError("Insufficient physical GPU memory, compute quota, or instance slots")
        self._allocations[allocation.allocation_id] = allocation
        self.revision += 1

    def release(self, allocation_id: str):
        if allocation_id not in self._allocations:
            raise KeyError(f"Unknown allocation: {allocation_id}")
        allocation = self._allocations.pop(allocation_id)
        self.revision += 1
        return allocation

    def snapshot(self):
        return {
            "data_kind": "synthetic_resource_state",
            "gpu_id": self.gpu_id,
            "profile_id": self.profile.profile_id,
            "revision": self.revision,
            "capacity_memory_mib": self.profile.modeled_memory_mib,
            "remaining_memory_mib": self.remaining_memory_mib,
            "remaining_compute_units": self.remaining_compute_units,
            "allocated_instances": self.allocated_instances,
            "remaining_instances": self.remaining_instances,
            "existing_task_types": {t: self.instances_of_type(t) for t in TASK_TYPES},
            "allocations": [asdict(a) for a in self._allocations.values()],
        }
