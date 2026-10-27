"""Mechanism fixtures only, not a benchmark or a GPU performance data source."""
import random
from ..profiles import load_profiles
from ..resources import GPUState, ResourceRequest
from .models import Job, AvailabilityEvent


def two_gpu_fleet():
    p = load_profiles()['rtx4090']
    return {f'mechanism-node/gpu-{i}': GPUState(f'mechanism-node/gpu-{i}', p) for i in range(2)}


def blocking_case(small_jobs=8):
    fleet = two_gpu_fleet(); cap = next(iter(fleet.values())).profile.modeled_memory_mib
    big = ResourceRequest(cap*60//100, 60, 1)
    spare = ResourceRequest(cap*20//100, 20, 1)
    jobs = [Job('00-background-1', 0., 0., 'training', 1., big, actual_service=1.),
            Job('01-background-2', 0., 0., 'training', 1., big, actual_service=2.),
            Job('10-large-two-gpu', 0., 0., 'training', 20., big, gpu_count=2, actual_service=3.),
            Job('30-spare-capacity', 1.1, 1.1, 'inference', 1., spare, actual_service=5.)]
    jobs.extend(Job(f'20-small-{i:02d}', float(i), float(i), 'inference', 1., big, actual_service=2.)
                for i in range(1, small_jobs+1))
    return fleet, tuple(jobs), ()


def cancellation_case():
    fleet, jobs, _ = blocking_case(4)
    from dataclasses import replace
    return fleet, tuple(replace(j, cancel_at=.75) if j.job_id=='10-large-two-gpu' else j for j in jobs), ()


def invalid_request_case():
    fleet = two_gpu_fleet(); cap = next(iter(fleet.values())).profile.modeled_memory_mib
    jobs = (Job('impossible', 0., 0., 'training', 20., ResourceRequest(cap+1, 10), gpu_count=2, actual_service=1.),
            Job('valid', 1., 1., 'inference', 1., ResourceRequest(cap//2, 50), actual_service=2.))
    return fleet, jobs, ()


def invalidation_case():
    fleet, jobs, _ = blocking_case(4)
    return fleet, jobs, (AvailabilityEvent(.75, 'mechanism-node/gpu-0', False),
                         AvailabilityEvent(3., 'mechanism-node/gpu-0', True))


def seeded_smoke(seed=20261023, count=18):
    rng = random.Random(seed); profiles = load_profiles(); fleet = {}
    for node, profile in (('a', 'rtx4090'), ('b', 'l40')):
        for i in range(2):
            key = f'{node}/gpu-{i}'; fleet[key] = GPUState(key, profiles[profile])
    minimum = min(g.profile.modeled_memory_mib for g in fleet.values()); jobs = []
    for i in range(count):
        arrival = round(rng.uniform(0, 6), 4); ready = arrival+round(rng.uniform(0, .4), 4)
        request = ResourceRequest(rng.randint(minimum//10, minimum*3//5), rng.randint(10,60), rng.randint(1,3))
        jobs.append(Job(f'seeded-{i:03d}', arrival, ready, rng.choice(('training','inference')),
            rng.choice((.5, 1., 4.)), request, gpu_count=rng.choice((1,1,2)),
            actual_service=round(rng.uniform(.2, 3.), 4)))
    return fleet, tuple(jobs), ()


SCENARIOS = dict(blocking=blocking_case, cancellation=cancellation_case,
                 impossible=invalid_request_case, invalidation=invalidation_case,
                 seeded_smoke=seeded_smoke)
