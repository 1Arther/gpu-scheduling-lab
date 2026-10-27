"""Deterministic discrete events; true synthetic durations remain private."""
from dataclasses import asdict
import heapq
from time import perf_counter
from .models import Config
from .policies import POLICIES, PROTECTING, Protection, order, overdue, response_ratio
from .placement import free, capacity
from .metrics import summarize

TERMINAL = {'completed', 'cancelled', 'rejected'}
PHASE = {'complete': 0, 'availability': 1, 'arrival': 2, 'ready': 3, 'cancel': 4, 'threshold': 5}


class Simulator:
    def __init__(self, cluster, jobs, placement, policy='hrrn', config=None, availability=()):
        if policy not in POLICIES:
            raise ValueError('Unknown queue policy')
        jobs = tuple(sorted(jobs, key=lambda j:j.job_id))
        if len({j.job_id for j in jobs}) != len(jobs):
            raise ValueError('Duplicate Job IDs')
        if len({(j.tenant, j.priority) for j in jobs}) > 1:
            raise ValueError('MVP supports one tenant/queue and one priority class; DRF integration is deferred')
        if cluster.running or any(g.allocations for g in cluster.gpus.values()):
            raise ValueError('Simulator needs an initially empty cluster; submit synthetic background Jobs at t=0')
        self.cluster, self.placement, self.policy = cluster, placement, policy
        self.config = config or Config()
        self._jobs = {j.job_id: j for j in jobs}
        self.public = {j.job_id: j.public() for j in jobs}
        self.clock = 0.; self._seq = 0; self._heap = []; self._ran = False
        self.protection = None; self.epoch = 0; self.events = []; self.timeline = []
        # Wall-clock diagnostics stay outside the reproducible simulation result.
        self.planning_diagnostics = []
        self.area_allocated = [0., 0., 0.]; self.area_protected = [0., 0., 0.]
        self.call_start = placement.calls; self.candidate_start = placement.candidates
        self.rows = {j.job_id: dict(job_id=j.job_id, arrival_at=j.arrival_at, ready_at=j.ready_at,
            task_type=j.task_type, estimated_service=j.estimated_service, estimate_source=j.estimate_source,
            gpu_count=j.gpu_count, request=asdict(j.request), status='not_arrived', start_at=None,
            finish_at=None, cancelled_at=None, released_at=None, allocation_active=False,
            gpu_ids=[], wait=None, waiting_reason=None) for j in jobs}
        for j in sorted(jobs, key=lambda j: j.job_id):
            for kind, when in (('arrival', j.arrival_at), ('ready', j.ready_at), ('threshold', j.ready_at+self.config.trigger(j))):
                self._push(when, kind, j.job_id)
            if j.cancel_at is not None:
                self._push(j.cancel_at, 'cancel', j.job_id)
        seen = set()
        for ev in sorted(availability, key=lambda e: (e.time, e.gpu_id)):
            if ev.gpu_id not in cluster.gpus or (ev.time, ev.gpu_id) in seen:
                raise ValueError('Unknown GPU or conflicting same-time availability events')
            seen.add((ev.time, ev.gpu_id)); self._push(ev.time, 'availability', ev.gpu_id, ev.online)
        self._record('initial')

    def _push(self, when, kind, key, value=None):
        self._seq += 1
        heapq.heappush(self._heap, (float(when), PHASE[kind], self._seq, kind, key, value))

    def _record(self, kind, job_id=None, **extra):
        if job_id is not None:
            job = self.public[job_id]
            extra.setdefault('estimate_source', job.estimate_source)
            extra.setdefault('tenant', job.tenant)
            extra.setdefault('priority', job.priority)
        self.events.append(dict(time=self.clock, kind=kind, job_id=job_id, state=self.cluster.snapshot(),
            cluster_version=self.cluster.version, protection_epoch=self.protection.epoch if self.protection else None,
            protection=self.protection.record() if self.protection else None, **extra))

    def _integrate(self, until):
        if until < self.clock:
            raise RuntimeError('Non-monotone simulator clock')
        end = min(until, self.config.observation_end)
        start = min(self.clock, self.config.observation_end)
        if end <= start:
            return
        allocated = [sum(capacity(g)[d]-free(g)[d] for g in self.cluster.gpus.values()) for d in range(3)]
        idle = self.protection.idle(self.cluster) if self.protection else {}
        protected = [sum(v[d] for v in idle.values()) for d in range(3)]
        for d in range(3):
            self.area_allocated[d] += allocated[d]*(end-start)
            self.area_protected[d] += protected[d]*(end-start)
        self.timeline.append(dict(start=start, end=end, allocated=allocated, protected_idle=protected,
            protection_epoch=self.protection.epoch if self.protection else None))

    def _clear(self, reason):
        if self.protection is not None:
            old = self.protection; self.protection = None
            self._record('protection_clear', old.job_id, reason=reason, previous=old.record())

    def _handle(self, kind, key, value):
        if kind == 'availability':
            self.cluster.set_online(key, value)
            self._record('availability', gpu_id=key, online=value,
                semantics='new allocations disabled/enabled; existing synthetic Jobs retain their scheduled release')
            return
        row = self.rows[key]
        if kind == 'complete':
            if row['status'] != 'running': return  # stale completion after cancellation
            self.cluster.release(key); row.update(status='completed', finish_at=self.clock,
                released_at=self.clock, allocation_active=False)
            self._record('complete', key); return
        if row['status'] in TERMINAL: return
        if kind == 'arrival':
            if not self.placement.structural(self.cluster, self.public[key]):
                row.update(status='rejected', waiting_reason='no_complete_empty_single_node_plan')
                self._record('reject', key, reason=row['waiting_reason'])
            else:
                row['status'] = 'not_ready'; self._record('arrival', key)
        elif kind == 'ready':
            row['status'] = 'waiting'; row['waiting_reason'] = 'awaiting_complete_resources'
            self._record('ready', key)
        elif kind == 'cancel':
            if row['status'] == 'running':
                self.cluster.release(key); row.update(released_at=self.clock, allocation_active=False)
            row.update(status='cancelled', cancelled_at=self.clock)
            self._record('cancel', key)
            if self.protection and self.protection.job_id == key: self._clear('owner_cancelled')
        elif kind == 'threshold':
            if row['status'] == 'waiting': self._record('threshold', key)

    def _start(self, job, plan, reason):
        before = self.protection.idle(self.cluster) if self.protection else {}
        owner = self.protection is not None and self.protection.job_id == job.job_id
        self.cluster.commit(job, plan, self.protection)
        row = self.rows[job.job_id]
        row.update(status='running', start_at=self.clock, wait=self.clock-job.ready_at,
                   gpu_ids=list(plan.gpu_ids), waiting_reason=None, allocation_active=True)
        # Actual duration is accessed only here, by the environment, not by policy.
        self._push(self.clock+self._jobs[job.job_id].actual_service, 'complete', job.job_id)
        after = self.protection.idle(self.cluster) if self.protection else {}
        if self.protection is not None and not owner:
            if any(any(a < b for a, b in zip(after[k], before[k])) for k in before):
                raise RuntimeError('A later Job consumed protected capacity')
        self._record('start', job.job_id, reason=reason, plan=asdict(plan),
            hrrn=response_ratio(job, self.clock, self.config), trigger=self.config.trigger(job),
            protected_idle_before={k:list(v) for k,v in before.items()},
            protected_idle_after={k:list(v) for k,v in after.items()})
        if owner: self._clear('owner_started')

    def _dispatch(self):
        if self.protection is not None:
            p = self.protection; job = self.public[p.job_id]
            if self.rows[p.job_id]['status'] != 'waiting': self._clear('owner_not_waiting')
            elif not self.cluster.valid(job, p.targets, empty=True): self._clear('target_unavailable_or_invalid')
        waiting = [j for k, j in self.public.items() if self.rows[k]['status'] == 'waiting']
        if self.policy in PROTECTING and self.protection is None:
            for job in sorted((j for j in waiting if overdue(j, self.clock, self.config)), key=lambda j:(j.ready_at,j.job_id)):
                plan = self.placement.choose(self.cluster, job, empty=True)
                if plan is None: continue  # temporarily unavailable; no permanent lock
                self.epoch += 1
                self.protection = Protection.create(self.epoch, job, plan, self.cluster, self.clock,
                    whole=self.policy == 'hrrn_whole_gpu')
                self._record('protection_create', job.job_id)
                break
        if self.protection is not None:
            job = self.public[self.protection.job_id]
            plan = self.cluster.fixed_plan(job, self.protection.targets, protection=self.protection)
            if plan is not None: self._start(job, plan, 'protected_owner')
        # Fixed clock and immutable public requests for this entire queue pass.
        for job in order(waiting, self.clock, self.policy, self.config):
            if self.rows[job.job_id]['status'] != 'waiting': continue
            if self.protection is not None and self.protection.job_id == job.job_id:
                self.rows[job.job_id]['waiting_reason'] = 'waiting_for_fixed_target_combination'
                self._record('bypass', job.job_id, reason=self.rows[job.job_id]['waiting_reason'],
                    hrrn=response_ratio(job,self.clock,self.config), trigger=self.config.trigger(job))
                continue
            plan = self.placement.choose(self.cluster, job, self.protection)
            if plan is not None: self._start(job, plan, 'queue_candidate')
            else:
                self.rows[job.job_id]['waiting_reason'] = 'capacity_or_protection_constraint'
                self._record('bypass', job.job_id, hrrn=response_ratio(job,self.clock,self.config),
                    trigger=self.config.trigger(job), reason=self.rows[job.job_id]['waiting_reason'])

    def run(self):
        if self._ran: raise RuntimeError('A Simulator instance can run only once')
        self._ran = True
        while self._heap:
            now = self._heap[0][0]; self._integrate(now); self.clock = now
            batch = []
            while self._heap and self._heap[0][0] == now: batch.append(heapq.heappop(self._heap))
            for _, _, _, kind, key, value in batch: self._handle(kind,key,value)
            started = perf_counter(); calls = self.placement.calls; candidates = self.placement.candidates
            self._dispatch()
            counts = dict(placement_calls=self.placement.calls-calls, candidates_checked=self.placement.candidates-candidates)
            self.planning_diagnostics.append(dict(time=self.clock, wall_seconds=perf_counter()-started, **counts))
            self._record('dispatch_end', **counts)
            if all(r['status'] in TERMINAL for r in self.rows.values()): break
        if self.clock < self.config.observation_end:
            self._integrate(self.config.observation_end); self.clock = self.config.observation_end
        if self.protection is not None:
            self._clear('simulation_ended')
        if self.cluster.running: raise RuntimeError('Missing synthetic completion event')
        self._record('end')
        metrics = summarize(self)
        return dict(policy=self.policy, model_version=self.placement.version, config=asdict(self.config),
            data_kind='synthetic_queue_mvp_not_GPU_performance', metrics=metrics,
            jobs=[dict(self.rows[k]) for k in sorted(self.rows)], timeline=self.timeline, events=self.events)
