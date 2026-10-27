"""Immutable input jobs and a policy view that cannot read true runtimes."""
from dataclasses import dataclass, asdict
import math
from ..resources import ResourceRequest, TASK_TYPES, positive_integer


def finite(value, name, minimum=0., strict=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f'{name} must be finite')
    if value < minimum or strict and value == minimum:
        raise ValueError(f'Invalid {name}')


@dataclass(frozen=True)
class PublicJob:
    job_id: str
    arrival_at: float
    ready_at: float
    task_type: str
    estimated_service: float
    request: ResourceRequest  # aggregate memory/core/slots on EACH selected GPU
    gpu_count: int = 1
    allowed_profiles: tuple[str, ...] = ()
    node: str | None = None
    tenant: str = 'default'
    priority: int = 0
    estimate_source: str = 'synthetic_submitted_estimate'


@dataclass(frozen=True)
class Job(PublicJob):
    actual_service: float = 1.
    cancel_at: float | None = None

    def __post_init__(self):
        if not isinstance(self.job_id, str) or not self.job_id.strip():
            raise ValueError('Nonempty Job ID required')
        finite(self.arrival_at, 'arrival_at'); finite(self.ready_at, 'ready_at', self.arrival_at)
        finite(self.estimated_service, 'estimated_service', strict=True)
        finite(self.actual_service, 'actual_service', strict=True)
        positive_integer(self.gpu_count, 'gpu_count')
        if not isinstance(self.request, ResourceRequest):
            raise ValueError('ResourceRequest required')
        if self.task_type not in TASK_TYPES:
            raise ValueError('Explicit training/inference type required')
        if not isinstance(self.allowed_profiles, tuple) or any(not isinstance(p, str) or not p for p in self.allowed_profiles):
            raise ValueError('allowed_profiles must be a tuple of profile IDs')
        if self.node is not None and (not isinstance(self.node, str) or not self.node):
            raise ValueError('node must be a node ID')
        if not isinstance(self.tenant, str) or not self.tenant or not isinstance(self.priority, int) or isinstance(self.priority, bool):
            raise ValueError('Invalid tenant/priority')
        if not isinstance(self.estimate_source, str) or not self.estimate_source:
            raise ValueError('Estimate source required')
        if self.cancel_at is not None:
            finite(self.cancel_at, 'cancel_at', self.arrival_at)

    def public(self):
        return PublicJob(**{k: getattr(self, k) for k in PublicJob.__dataclass_fields__})


@dataclass(frozen=True)
class Config:
    tau: float = .1
    protection_absolute_wait: float = .5
    protection_kappa: float = 2.
    evaluation_long_wait: float = 4.  # separate from the algorithm trigger
    observation_end: float = 12.

    def __post_init__(self):
        for name in ('tau', 'protection_absolute_wait', 'protection_kappa', 'evaluation_long_wait', 'observation_end'):
            finite(getattr(self, name), name, strict=True)

    def trigger(self, job):
        return min(self.protection_absolute_wait, self.protection_kappa * max(job.estimated_service, self.tau))


@dataclass(frozen=True)
class AvailabilityEvent:
    time: float
    gpu_id: str
    online: bool

    def __post_init__(self):
        finite(self.time, 'availability time')
        if not isinstance(self.gpu_id, str) or not self.gpu_id or not isinstance(self.online, bool):
            raise ValueError('Invalid availability event')


def job_record(job):
    return asdict(job)


def job_from_record(row):
    data = dict(row); data['request'] = ResourceRequest(**data['request'])
    data['allowed_profiles'] = tuple(data.get('allowed_profiles', ()))
    return Job(**data)
