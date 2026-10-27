"""Reuse GPUState admission and the unmodified frozen RF scoring service.

Jobs request identical aggregate amounts on distinct GPUs of ONE node. This
MVP is not a general multi-Pod graph or a real HAMi binding implementation.
"""
from copy import deepcopy
from dataclasses import dataclass
from itertools import combinations
from ..resources import GPUState, Allocation
from ..runtime.bundle import DEFAULT_BUNDLE
from ..runtime.service import SuitabilityService
from ..runtime.pods import DeviceRequest, PodSpec
from ..runtime.schema import HAMI_QUOTA, MEMORY_REFERENCE_MAPPING


def free(gpu):
    return (gpu.remaining_memory_mib, gpu.remaining_compute_units, gpu.remaining_instances)


def capacity(gpu):
    return (gpu.profile.modeled_memory_mib, 100, gpu.max_instances)


def amount(job):
    r = job.request
    return (r.memory_mib, r.compute_units, r.instances)


@dataclass(frozen=True)
class Plan:
    node: str
    gpu_ids: tuple[str, ...]
    score: float
    revisions: tuple[tuple[str, int], ...]
    protection_epoch: int | None = None


class Cluster:
    def __init__(self, fleet, nodes=None):
        if not fleet:
            raise ValueError('Nonempty fleet required')
        if any(not isinstance(g, GPUState) or g.allocations or key != g.gpu_id for key, g in fleet.items()):
            raise ValueError('Fresh keyed GPUState fleet required')
        self.gpus = deepcopy(fleet)
        self.nodes = dict(nodes) if nodes is not None else {key: key.rsplit('/gpu-', 1)[0] for key in fleet}
        if set(self.nodes) != set(fleet) or any(not isinstance(n, str) or not n for n in self.nodes.values()):
            raise ValueError('Every GPU needs an explicit node')
        self.online = {key: True for key in fleet}
        self.running = {}
        self.version = 0

    def valid(self, job, ids, *, empty=False, online=True):
        if len(ids) != job.gpu_count or len(set(ids)) != len(ids) or any(k not in self.gpus for k in ids):
            return False
        if len({self.nodes[k] for k in ids}) != 1 or job.node is not None and self.nodes[ids[0]] != job.node:
            return False
        for key in ids:
            g = self.gpus[key]
            if online and not self.online[key] or job.allowed_profiles and g.profile.profile_id not in job.allowed_profiles:
                return False
            view = GPUState(key, g.profile, g.max_instances) if empty else g
            if not view.can_allocate(job.request):
                return False
            # Same native full-core rule as the frozen service.
            if job.request.compute_units == 100 and view.allocated_instances:
                return False
        return True

    def fixed_plan(self, job, ids, score=0., protection=None):
        if not self.valid(job, ids):
            return None
        return Plan(self.nodes[ids[0]], tuple(ids), score, tuple((k, self.gpus[k].revision) for k in ids),
                    protection.epoch if protection else None)

    def commit(self, job, plan, protection=None):
        if job.job_id in self.running or not self.valid(job, plan.gpu_ids) or plan.node != self.nodes[plan.gpu_ids[0]]:
            raise ValueError('Invalid or incomplete full-Job plan')
        if plan.revisions != tuple((k, self.gpus[k].revision) for k in plan.gpu_ids):
            raise ValueError('Stale resource revision')
        if plan.protection_epoch != (protection.epoch if protection else None):
            raise ValueError('Stale protection epoch')
        if protection is not None and not protection.allows(self, job, plan.gpu_ids):
            raise ValueError('Allocation would consume protected capacity')
        # Stage every GPU using the reused ledger, publish only after ALL pass.
        staged = {}
        for key in plan.gpu_ids:
            state = deepcopy(self.gpus[key])
            state.allocate(Allocation(job.job_id, job.task_type, job.request))
            staged[key] = state
        self.gpus.update(staged)
        self.running[job.job_id] = plan.gpu_ids
        self.version += 1

    def release(self, job_id):
        if job_id not in self.running:
            raise KeyError(f'Job not allocated: {job_id}')
        ids = self.running[job_id]
        staged = {k: deepcopy(self.gpus[k]) for k in ids}
        for state in staged.values():
            state.release(job_id)
        self.gpus.update(staged); del self.running[job_id]; self.version += 1

    def set_online(self, key, online):
        if key not in self.gpus or not isinstance(online, bool):
            raise ValueError('Invalid device availability update')
        self.online[key] = online; self.version += 1

    def snapshot(self):
        return {k: {**g.snapshot(), 'node': self.nodes[k], 'online': self.online[k]} for k, g in sorted(self.gpus.items())}


class FrozenPlacement:
    def __init__(self, bundle_dir=DEFAULT_BUNDLE, service=None):
        self.service = service or SuitabilityService(bundle_dir)
        if self.service.model is None:
            raise ValueError(f'Frozen RF unavailable: {self.service.model_error}')
        self.version = self.service.version
        self.calls = 0; self.candidates = 0

    def snapshot_for_rf(self, cluster, key, empty=False):
        source = cluster.gpus[key]
        g = GPUState(key, source.profile, source.max_instances) if empty else source
        if g.profile.profile_id not in self.service.catalog:
            raise ValueError(f'Profile outside the existing frozen catalog: {g.profile.profile_id}')
        return dict(uuid=key, node=cluster.nodes[key], device_id=g.profile.profile_id,
            model=g.profile.name, hardware=self.service.catalog[g.profile.profile_id].values(),
            logical_memory_capacity_mib=g.profile.modeled_memory_mib,
            model_memory_mapping=MEMORY_REFERENCE_MAPPING, quota_semantics=HAMI_QUOTA,
            dynamic=dict(free_memory_mib=g.remaining_memory_mib, free_compute_quota=g.remaining_compute_units,
                allocated_vgpu_count=g.allocated_instances, running_training_count=g.instances_of_type('training'),
                running_inference_count=g.instances_of_type('inference'), running_unknown_count=0),
            healthy=cluster.online[key], managed=True, plugin='volcano-vgpu', backend='CUDA', sharing_mode='hami-core',
            max_instances=g.max_instances, revision=g.revision, node_labels={})

    def structural(self, cluster, job):
        # No scoring, no mutation, and unavailable devices are still hardware.
        for node in sorted(set(cluster.nodes.values())):
            ids = [k for k in sorted(cluster.gpus) if cluster.nodes[k] == node]
            for combo in combinations(ids, job.gpu_count):
                if cluster.valid(job, combo, empty=True, online=False):
                    return True
        return False

    def plans(self, cluster, job, protection=None, *, empty=False):
        self.calls += 1
        request = DeviceRequest('job', 1, job.request.memory_mib, None, job.request.compute_units)
        pod = PodSpec(job.job_id, job.job_id, 'queue-mvp', job.task_type, 'CUDA', (request,), job.node, {}, {})
        views = [self.snapshot_for_rf(cluster, k, empty) for k in sorted(cluster.gpus)]
        ranking = self.service.rank_gpus(pod, views, strategy='dynamic_rf')
        by_id = {r['gpu_uuid']: r for r in ranking['eligible_gpus']}
        result = []
        for node in sorted(set(cluster.nodes.values())):
            eligible = [k for k in sorted(by_id) if cluster.nodes[k] == node]
            for combo in combinations(eligible, job.gpu_count):
                self.candidates += 1
                if not cluster.valid(job, combo, empty=empty):
                    continue
                if not empty and protection is not None and protection.job_id != job.job_id and not protection.allows(cluster, job, combo):
                    continue
                result.append(Plan(node, combo, sum(by_id[k]['score'] for k in combo) / len(combo),
                    tuple((k, cluster.gpus[k].revision) for k in combo), protection.epoch if protection else None))
        return sorted(result, key=lambda p: (-p.score, p.node, p.gpu_ids))

    def choose(self, cluster, job, protection=None, *, empty=False):
        plans = self.plans(cluster, job, protection, empty=empty)
        return plans[0] if plans else None
