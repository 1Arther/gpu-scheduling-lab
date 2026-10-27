"""Time-frozen queue ordering and one persistent complete protection plan."""
from dataclasses import dataclass
from .placement import free, capacity, amount

POLICIES = ('fifo', 'sjf', 'hrrn', 'hrrn_age', 'hrrn_protect', 'hrrn_whole_gpu')
PROTECTING = ('hrrn_protect', 'hrrn_whole_gpu')


def response_ratio(job, now, config):
    return 1. + (now - job.ready_at) / max(job.estimated_service, config.tau)


def overdue(job, now, config):
    return now >= job.ready_at + config.trigger(job)


def order(jobs, now, policy, config):
    if policy not in POLICIES:
        raise ValueError('Unknown queue policy')
    def key(job):
        if policy == 'fifo': return (job.ready_at, job.job_id)
        if policy == 'sjf': return (job.estimated_service, job.ready_at, job.job_id)
        if policy == 'hrrn_age' and overdue(job, now, config):
            return (0, job.ready_at, job.ready_at, job.job_id)
        prefix = (1,) if policy == 'hrrn_age' else ()
        return prefix + (-response_ratio(job, now, config), job.ready_at, job.job_id)
    return sorted(jobs, key=key)


@dataclass(frozen=True)
class Protection:
    epoch: int
    job_id: str
    node: str
    targets: tuple[str, ...]
    required: tuple[tuple[str, tuple[int, int, int]], ...]
    created_at: float
    mode: str

    @classmethod
    def create(cls, epoch, job, plan, cluster, now, whole=False):
        if not cluster.valid(job, plan.gpu_ids, empty=True):
            raise ValueError('Protection needs a complete structurally feasible plan')
        required = []
        for key in plan.gpu_ids:
            reserve = capacity(cluster.gpus[key]) if whole else amount(job)
            # Full-core native requests require an empty GPU, not just core100.
            if job.request.compute_units == 100:
                reserve = (reserve[0], reserve[1], cluster.gpus[key].max_instances)
            required.append((key, reserve))
        return cls(epoch, job.job_id, plan.node, plan.gpu_ids, tuple(required), now, 'whole_gpu' if whole else 'vgpu_capacity')

    def idle(self, cluster):
        return {k: tuple(min(f, r) for f, r in zip(free(cluster.gpus[k]), reserve)) for k, reserve in self.required}

    def allows(self, cluster, job, ids):
        if job.job_id == self.job_id:
            return tuple(ids) == self.targets
        required = dict(self.required); demand = amount(job)
        return all(k not in required or all(d <= max(0, f-r) for d, f, r in zip(demand, free(cluster.gpus[k]), required[k])) for k in ids)

    def record(self):
        return dict(epoch=self.epoch, job_id=self.job_id, node=self.node, targets=list(self.targets),
                    required={k: list(r) for k, r in self.required}, created_at=self.created_at, mode=self.mode)
