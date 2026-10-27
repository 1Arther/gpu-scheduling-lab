"""Explicit time windows, queue censoring and logical (not measured) resources."""
import numpy as np
from .placement import capacity

NAMES = ('memory_mib', 'compute_units', 'instance_slots')


def summarize(env):
    rows = list(env.rows.values()); end = env.config.observation_end
    ready = [r for r in rows if r['ready_at'] <= env.clock and r['status'] != 'rejected'
             and (r['cancelled_at'] is None or r['cancelled_at'] >= r['ready_at'])]
    started = [r for r in ready if r['start_at'] is not None]
    censored = [r for r in ready if r['start_at'] is None]
    waits = [r['wait'] for r in started]
    observations = waits + [max(0.,(r['cancelled_at'] if r['cancelled_at'] is not None else env.clock)-r['ready_at']) for r in censored]
    long_count = sum(w >= env.config.evaluation_long_wait for w in observations)
    # Pending jobs with observed age below threshold have unknown eventual fate.
    unknown = sum(r['cancelled_at'] is None and env.clock-r['ready_at'] < env.config.evaluation_long_wait for r in censored)
    totals = [sum(capacity(g)[d] for g in env.cluster.gpus.values()) for d in range(3)]
    rate = lambda values: {n: values[d]/(totals[d]*end) for d,n in enumerate(NAMES)}
    a, p = env.area_allocated, env.area_protected
    all_clear = all(not g.allocations for g in env.cluster.gpus.values())
    return dict(jobs=len(rows), eligible_ready_jobs=len(ready), started_jobs=len(started),
        completed_count=sum(r['finish_at'] is not None and r['finish_at'] <= end for r in rows),
        completed_total=sum(r['status']=='completed' for r in rows), cancelled_count=sum(r['status']=='cancelled' for r in rows),
        rejected_count=sum(r['status']=='rejected' for r in rows), pending_count=sum(r['status']=='waiting' for r in rows),
        mean_wait=float(np.mean(waits)) if waits and not censored else None,
        p95_wait=float(np.quantile(waits,.95)) if waits and not censored else None,
        mean_wait_started=float(np.mean(waits)) if waits else None,
        p95_wait_started=float(np.quantile(waits,.95)) if waits else None,
        observed_mean_queue_wait=float(np.mean(observations)) if observations else None,
        censored_wait_count=len(censored), wait_metric_scope='all ready non-rejected Jobs; primary mean/P95 null when any never-started wait is censored',
        long_wait_ratio=long_count/len(ready) if ready and not unknown else None,
        long_wait_ratio_lower_bound=long_count/len(ready) if ready else None,
        long_wait_ratio_upper_bound=(long_count+unknown)/len(ready) if ready else None,
        evaluation_long_wait=env.config.evaluation_long_wait,
        observation_end=end, drained_until=env.clock,
        logical_allocation_rate=rate(a), logical_protection_reservation_rate=rate(p),
        logical_reserved_rate=rate([x+y for x,y in zip(a,p)]),
        protected_idle_capacity_time=dict(zip(NAMES,p)), allocated_capacity_time=dict(zip(NAMES,a)),
        rate_semantics='allocated logical commitments + currently idle protected capacity / configured capacity-time; no measured GPU utilization',
        placement_calls=env.placement.calls-env.call_start, candidates_checked=env.placement.candidates-env.candidate_start,
        protection_epochs=env.epoch, all_resources_released=all_clear, final_protection_cleared=env.protection is None)
