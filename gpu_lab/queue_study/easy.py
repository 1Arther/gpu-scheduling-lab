"""Single-head EASY reservation with exact vector/GPU-combination backfill.

Only PublicJob, observed allocations and estimated deadlines enter this module.
No actual duration, future arrival, simulator heap or completion event is read.
"""
from copy import deepcopy
from dataclasses import dataclass, asdict
from ..queue_sim.models import PublicJob
from ..queue_sim.policies import order
from ..resources import Allocation


@dataclass(frozen=True)
class Forecast:
    job: PublicJob
    gpu_ids: tuple[str, ...]
    start: float
    expected_end: float
    extensions: int = 0

    def record(self):
        return dict(job_id=self.job.job_id, gpu_ids=list(self.gpu_ids), start=self.start,
                    expected_end=self.expected_end, estimated_service=self.job.estimated_service,
                    extensions=self.extensions)


@dataclass(frozen=True)
class Reservation:
    job_id: str
    targets: tuple[str, ...]
    expected_start: float
    score: float

    def record(self):
        return asdict(self)


def project(cluster, forecasts, when, candidate=None):
    """Read-only estimated release calendar; never alters the real ledger."""
    view = deepcopy(cluster)
    if candidate is not None:
        job, ids, predicted_end = candidate
        if predicted_end > when:
            for key in ids:
                view.gpus[key].allocate(Allocation(job.job_id, job.task_type, job.request))
    for jid, f in forecasts.items():
        if f.expected_end <= when and jid in view.running:
            view.release(jid)
    return view


class EasyScheduler:
    """Keep one blocked HRRN head, find its earliest complete calendar slot.

    Backfill may overlap the reservation if it fits the remaining vGPU vectors;
    it need not finish before the head starts. No artificial whole-card hold.
    Reservations are recomputed on observed events, early finishes and deadline
    extensions; changes and misses are explicitly logged by the environment.
    """
    def __init__(self):
        self.reservation = None

    def earliest(self, cluster, job, forecasts, placement, now):
        for when in sorted({now, *(f.expected_end for f in forecasts.values() if f.expected_end > now)}):
            view = project(cluster, forecasts, when)
            # Avoid RF evaluation on calendar instants with no complete fit.
            if not any(view.valid(job, combo, online=True)
                       for combo in self.combinations(view, job)):
                continue
            plan = placement.choose(view, job)
            if plan is not None:
                return Reservation(job.job_id, plan.gpu_ids, when, plan.score)
        return None

    @staticmethod
    def combinations(cluster, job):
        from itertools import combinations
        for node in sorted(set(cluster.nodes.values())):
            yield from combinations([k for k in sorted(cluster.gpus) if cluster.nodes[k] == node], job.gpu_count)

    @staticmethod
    def safe(cluster, owner, reservation, forecasts, candidate, plan, now, tau):
        end = now + max(candidate.estimated_service, tau)
        future = project(cluster, forecasts, reservation.expected_start, (candidate, plan.gpu_ids, end))
        return future.valid(owner, reservation.targets)

    def dispatch(self, cluster, waiting, forecasts, placement, config, now, start, emit):
        pending = {j.job_id:j for j in waiting}
        old = self.reservation
        if old is not None and (old.job_id not in pending or not cluster.valid(pending[old.job_id], old.targets, empty=True)):
            emit('easy_clear', old.job_id, previous=old.record(), reason='owner_gone_or_target_invalid')
            self.reservation = None
        if self.reservation is not None:
            owner = pending[self.reservation.job_id]
            revised = self.earliest(cluster, owner, forecasts, placement, now)
            if revised != self.reservation:
                previous = self.reservation
                emit('easy_reservation_update', owner.job_id, previous=previous.record(),
                     reservation=revised.record() if revised else None,
                     delayed=revised is not None and revised.expected_start > previous.expected_start + 1e-9,
                     forecasts=[f.record() for f in forecasts.values()])
                self.reservation = revised
        # Start the reserved owner before admitting any backfill, when truly fit.
        if self.reservation is not None:
            owner = pending[self.reservation.job_id]
            plan = cluster.fixed_plan(owner, self.reservation.targets, self.reservation.score)
            if plan is not None:
                start(owner, plan, 'easy_reserved_owner')
                emit('easy_clear', owner.job_id, previous=self.reservation.record(), reason='owner_started')
                self.reservation = None
                del pending[owner.job_id]
        for job in order(list(pending.values()), now, 'hrrn', config):
            if self.reservation is not None and self.reservation.job_id == job.job_id:
                continue
            plans = placement.plans(cluster, job)
            if self.reservation is None:
                if plans:
                    start(job, plans[0], 'easy_queue_head')
                    continue
                reservation = self.earliest(cluster, job, forecasts, placement, now)
                if reservation is not None:
                    self.reservation = reservation
                    emit('easy_reserve', job.job_id, reservation=reservation.record(),
                         forecasts=[f.record() for f in forecasts.values()])
                continue
            owner = pending[self.reservation.job_id]
            chosen = next((p for p in plans if self.safe(cluster, owner, self.reservation, forecasts, job, p, now, config.tau)), None)
            if chosen is not None:
                emit('easy_backfill_check', job.job_id, owner_id=owner.job_id,
                     reservation=self.reservation.record(), gpu_ids=list(chosen.gpu_ids),
                     candidate_expected_end=now+max(job.estimated_service,config.tau),
                     forecasts=[f.record() for f in forecasts.values()])
                start(job, chosen, 'easy_safe_backfill')
