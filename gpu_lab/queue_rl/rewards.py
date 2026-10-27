"""Undiscounted finite-episode synthetic-time cost (gamma=lambda=1)."""
from dataclasses import dataclass
from ..queue_sim.placement import capacity
from ..queue_study.metrics import size_class

@dataclass(frozen=True)
class RewardSpec:
    waiting: float=1.
    long_wait: float=1.
    large: float=.5
    idle: float=.5
    job_scale: float=24.
    time_scale: float=5.

def interval_cost(waiting, cluster, protection, start, end, threshold, spec):
    dt=end-start
    if dt<=0:return dict(waiting=0.,long_wait=0.,large=0.,idle=0.)
    norm=spec.job_scale*spec.time_scale
    long_area=sum(max(0.,end-max(start,j.ready_at+threshold)) for j in waiting)
    large_n=sum(size_class(j,cluster.gpus)=='large' for j in waiting)
    protected=protection.idle(cluster) if protection else {}
    fraction=sum(sum(v[d] for v in protected.values())/sum(capacity(g)[d] for g in cluster.gpus.values()) for d in range(3))/3
    return dict(waiting=spec.waiting*dt*len(waiting)/norm,long_wait=spec.long_wait*long_area/norm,
        large=spec.large*dt*large_n/norm,idle=spec.idle*dt*fraction/spec.time_scale)
