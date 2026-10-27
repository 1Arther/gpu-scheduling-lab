"""Suspend the frozen event engine only at eligible protection decisions."""
from dataclasses import asdict
import heapq
from time import perf_counter
from ..queue_study.simulator import StudySimulator
from ..queue_sim.engine import TERMINAL
from ..queue_sim.policies import Protection
from ..queue_sim.metrics import summarize
from .actions import decision_context, masks
from .rewards import RewardSpec, interval_cost

class DecisionSimulator(StudySimulator):
    def __init__(self,*args,reward_spec=None,variant='joint',**kwargs):
        self.cost_parts=dict(waiting=0.,long_wait=0.,large=0.,idle=0.)
        self.reward_spec=reward_spec or RewardSpec();self.variant=variant
        self.context=None;self.done=False;self.result=None;self.decisions=[]
        self._pending_timing=None
        self._pause_started=None
        self.controller_diagnostics=[]
        super().__init__(*args,policy='hrrn',**kwargs)

    @property
    def cost(self):return sum(self.cost_parts.values())

    def _integrate(self,until):
        waiting=[j for k,j in self.public.items() if self.rows[k]['status']=='waiting']
        parts=interval_cost(waiting,self.cluster,self.protection,self.clock,until,
                            self.config.evaluation_long_wait,self.reward_spec)
        for k,v in parts.items():self.cost_parts[k]+=v
        super()._integrate(until)

    def _clean_guard(self):
        p=self.protection
        if p:
            if self.rows[p.job_id]['status']!='waiting':self._clear('owner_not_waiting')
            elif not self.cluster.valid(self.public[p.job_id],p.targets,empty=True):
                self._clear('target_unavailable_or_invalid')

    def _finish_dispatch(self):
        started,calls,candidates=self._pending_timing
        super()._dispatch()  # ordinary HRRN, respecting an existing pinned guard
        counts=dict(placement_calls=self.placement.calls-calls,candidates_checked=self.placement.candidates-candidates)
        self.planning_diagnostics.append(dict(time=self.clock,wall_seconds=perf_counter()-started,**counts))
        self._record('dispatch_end',**counts);self._pending_timing=None

    def advance(self,action=None,reason=None):
        if self.done:return None
        if self.context is not None:
            # Environment/policy wall time while suspended is measured separately.
            started,calls,candidates=self._pending_timing
            self._pending_timing=(started+perf_counter()-self._pause_started,calls,candidates)
            valid=masks(self.context,self.variant)
            legal=isinstance(action,(int,)) and 0<=action<4 and valid[action]
            chosen=action if legal else 0
            record=dict(context=self.context,mask=valid,proposed_action=int(action) if isinstance(action,int) else str(action),
                        action=chosen,reason=reason or ('accepted' if legal else 'illegal_safe_fallback'))
            self.decisions.append(record);self._record('rl_decision',self.context['owner']['job_id'],decision=record)
            if chosen:
                j=self.public[self.context['owner']['job_id']];combo=self.context['combos'][chosen-1]
                # Re-query the unchanged frozen backend before constructing a guard.
                candidates=self.placement.plans(self.cluster,j,empty=True)
                plan=next((p for p in candidates if list(p.gpu_ids)==combo['targets']),None)
                if plan is None:raise RuntimeError('Decision snapshot unexpectedly changed')
                self.epoch+=1;self.protection=Protection.create(self.epoch,j,plan,self.cluster,self.clock)
                self._record('protection_create',j.job_id)
            self.context=None;self._finish_dispatch()
        elif action is not None:raise ValueError('No decision pending')
        while self._heap:
            if all(r['status'] in TERMINAL for r in self.rows.values()):break
            now=self._heap[0][0];self._integrate(now);self.clock=now
            batch=[]
            while self._heap and self._heap[0][0]==now:batch.append(heapq.heappop(self._heap))
            for _,_,_,kind,key,value in batch:self._handle(kind,key,value)
            self._pending_timing=(perf_counter(),self.placement.calls,self.placement.candidates)
            self._clean_guard()
            if self.protection is None:
                waiting=[j for k,j in self.public.items() if self.rows[k]['status']=='waiting']
                self.context=decision_context(self.cluster,waiting,self.forecasts,self.placement,self.config,self.clock)
                if self.context is not None:
                    self._pause_started=perf_counter();return self.context
            self._finish_dispatch()
        self._finalize();return None

    def _finalize(self):
        if self.clock<self.config.observation_end:
            self._integrate(self.config.observation_end);self.clock=self.config.observation_end
        if self.protection:self._clear('simulation_ended')
        if self.cluster.running or any(r['status'] not in TERMINAL for r in self.rows.values()):
            raise RuntimeError('Episode did not drain; refusing to drop unfinished jobs')
        self._record('end');self.done=True
        self.result=dict(policy='hrrn_adaptive',model_version=self.placement.version,config=asdict(self.config),
            data_kind='synthetic_stage3_not_measured_GPU_performance',metrics=summarize(self),
            jobs=[dict(self.rows[k]) for k in sorted(self.rows)],timeline=self.timeline,events=self.events,
            reward_cost=self.cost,reward_parts=self.cost_parts,decisions=self.decisions)

    def run_policy(self,policy):
        self.advance()
        while not self.done:
            started=perf_counter()
            action,reason=policy(self.context,masks(self.context,self.variant))
            self.controller_diagnostics.append(1000*(perf_counter()-started))
            self.advance(int(action),reason)
        return self.result
