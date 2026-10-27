"""Extend the frozen phase-one event engine, keeping its physical backend."""
from dataclasses import replace
import heapq
from ..queue_sim.engine import Simulator
from ..queue_sim.policies import POLICIES as BASE_POLICIES
from ..queue_sim.placement import capacity, free
from .easy import EasyScheduler, Forecast

POLICIES = BASE_POLICIES + ('hrrn_easy',)


class StudySimulator(Simulator):
    def __init__(self, cluster, jobs, placement, policy='hrrn', config=None, availability=()):
        if policy not in POLICIES:
            raise ValueError('Unknown study policy')
        self.forecasts = {}
        self.easy = EasyScheduler()
        super().__init__(cluster, jobs, placement, 'hrrn' if policy=='hrrn_easy' else policy, config, availability)
        self.policy = policy

    def _push(self, when, kind, key, value=None):
        if kind != 'estimate_tick':
            return super()._push(when,kind,key,value)
        self._seq += 1
        heapq.heappush(self._heap, (float(when),6,self._seq,kind,key,value))

    def _handle(self, kind, key, value):
        if kind == 'estimate_tick':
            f = self.forecasts.get(key)
            if f is not None and f.expected_end == value and f.expected_end <= self.clock:
                # Alive at the submitted deadline: extend from observed elapsed
                # time, without inspecting the hidden actual completion event.
                end = self.clock + max(f.job.estimated_service, self.clock-f.start, self.config.tau)
                self.forecasts[key] = replace(f, expected_end=end, extensions=f.extensions+1)
                self._record('estimate_update',key,previous=f.record(),forecast=self.forecasts[key].record())
                self._push(end,'estimate_tick',key,end)
            return
        super()._handle(kind,key,value)
        if kind in ('complete','cancel'):
            self.forecasts.pop(key,None)

    def _start(self, job, plan, reason):
        super()._start(job,plan,reason)
        end = self.clock+max(job.estimated_service,self.config.tau)
        self.forecasts[job.job_id] = Forecast(job,tuple(plan.gpu_ids),self.clock,end)
        self._push(end,'estimate_tick',job.job_id,end)

    def _dispatch(self):
        if self.policy == 'hrrn_easy':
            waiting=[j for k,j in self.public.items() if self.rows[k]['status']=='waiting']
            self.easy.dispatch(self.cluster,waiting,self.forecasts,self.placement,self.config,self.clock,self._start,self._record)
            return
        super()._dispatch()

    def _record(self, kind, job_id=None, **extra):
        # Spatial cost diagnostics use only current free capacity/forecasts.
        if kind=='protection_create':
            p=self.protection;owner=self.public[job_id]
            idle=p.idle(self.cluster)
            extra['idle_fraction_of_required']=sum(v[1] for v in idle.values())/sum(r[1] for _,r in p.required)
            forecast=self.easy.earliest(self.cluster,owner,self.forecasts,self.placement,self.clock)
            extra['estimated_target_delay']=None
            # Fixed-target estimate, not the best alternative, is the actual
            # wait cost of the pinned spatial plan.
            from .easy import project
            for when in sorted({self.clock,*(f.expected_end for f in self.forecasts.values())}):
                if project(self.cluster,self.forecasts,when).valid(owner,p.targets):
                    extra['estimated_target_delay']=when-self.clock;break
            extra['earliest_any_combination_delay']=forecast.expected_start-self.clock if forecast else None
        super()._record(kind,job_id,**extra)

    def run(self):
        result=super().run()
        result['data_kind']='synthetic_stage2_not_measured_GPU_performance'
        return result
