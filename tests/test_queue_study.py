from dataclasses import replace
import json
import pytest
from gpu_lab.resources import ResourceRequest
from gpu_lab.queue_sim import Job,Config,Cluster,FrozenPlacement,AvailabilityEvent
from gpu_lab.queue_sim.scenarios import blocking_case,two_gpu_fleet,cancellation_case,invalidation_case
from gpu_lab.queue_study import StudySimulator,POLICIES
from gpu_lab.queue_study.easy import Forecast,EasyScheduler
from gpu_lab.queue_study.placement import MemoizedPlacement
from gpu_lab.queue_study.audit import verify_study
from gpu_lab.queue_study.workloads import generate,Condition,TEST_SEEDS,DEV_SEEDS


@pytest.fixture(scope='module')
def placement():return MemoizedPlacement()


@pytest.mark.parametrize('policy',POLICIES)
def test_seven_shared_backend_atomic_release(placement,policy,monkeypatch):
    from gpu_lab.queue_sim.placement import Cluster as Backend
    original=Backend.commit;calls=[]
    def checked(self,*a,**kw):calls.append(a[0].job_id);return original(self,*a,**kw)
    monkeypatch.setattr(Backend,'commit',checked)
    fleet,jobs,av=blocking_case()
    r=StudySimulator(Cluster(fleet),jobs,placement,policy,availability=av).run()
    assert verify_study(r,jobs,fleet)['success'] and len(calls)==len(jobs)
    assert all(len(j['gpu_ids'])==j['gpu_count'] for j in r['jobs'])


def test_easy_accepts_short_and_long_spare_but_rejects_delay(placement):
    fleet=two_gpu_fleet();cluster=Cluster(fleet);easy=EasyScheduler();cap=next(iter(fleet.values())).profile.modeled_memory_mib
    bg=Job('bg',0,0,'training',4,ResourceRequest(cap*6//10,60)).public()
    key=sorted(fleet)[1];cluster.commit(bg,cluster.fixed_plan(bg,(key,)))
    forecast={'bg':Forecast(bg,(key,),0,4)}
    owner=replace(bg,job_id='owner',gpu_count=2)
    r=easy.earliest(cluster,owner,forecast,placement,1)
    assert r.expected_start==4 and len(r.targets)==2
    short=replace(bg,job_id='short',estimated_service=2)
    plan=placement.choose(cluster,short)
    assert easy.safe(cluster,owner,r,forecast,short,plan,1,.1)
    long=replace(short,estimated_service=10)
    assert not easy.safe(cluster,owner,r,forecast,long,plan,1,.1)
    spare=replace(long,request=ResourceRequest(cap//5,20,1))
    assert easy.safe(cluster,owner,r,forecast,spare,placement.choose(cluster,spare),1,.1)


@pytest.mark.parametrize('dimension',('memory','compute','slots'))
def test_easy_backfill_honors_each_dimension(placement,dimension):
    fleet=two_gpu_fleet();cluster=Cluster(fleet);cap=next(iter(fleet.values())).profile.modeled_memory_mib
    owner=Job('owner',0,0,'training',10,ResourceRequest(cap//5,20,2),gpu_count=2).public()
    from gpu_lab.queue_study.easy import Reservation
    reservation=Reservation('owner',tuple(sorted(fleet)),3,0)
    values={'memory':ResourceRequest(cap*9//10,10,1),'compute':ResourceRequest(10,90,1),'slots':ResourceRequest(10,10,9)}
    other=Job('other',0,0,'inference',10,values[dimension]).public()
    assert not EasyScheduler.safe(cluster,owner,reservation,{},other,placement.choose(cluster,other),0,.1)


def test_overrun_and_early_completion_recalculate_without_oracle(placement):
    fleet,jobs,av=blocking_case()
    r=StudySimulator(Cluster(fleet),jobs,placement,'hrrn_easy').run()
    audit=verify_study(r,jobs,fleet)
    assert audit['estimate_extensions_checked']>0
    assert any(e['kind']=='easy_reservation_update' for e in r['events'])
    assert all('actual_service' not in str(e.get('forecasts',[])) for e in r['events'])


@pytest.mark.parametrize('factory',(cancellation_case,invalidation_case))
def test_easy_cancel_invalidate_and_recover(placement,factory):
    f,j,a=factory();r=StudySimulator(Cluster(f),j,placement,'hrrn_easy',availability=a).run()
    assert verify_study(r,j,f)['success']
    assert any(e['kind']=='easy_clear' for e in r['events'])


def test_prefix_nonanticipation(placement):
    f=two_gpu_fleet();req=ResourceRequest(14000,60)
    jobs=(Job('a',0,0,'training',2,req,actual_service=5),
          Job('b',0,0,'training',2,req,actual_service=6),
          Job('c',.1,.1,'inference',1,req,gpu_count=2,actual_service=1))
    other=tuple(replace(j,actual_service=j.actual_service+100) for j in jobs)
    a=StudySimulator(Cluster(f),jobs,placement,'hrrn_easy').run()
    b=StudySimulator(Cluster(f),other,placement,'hrrn_easy').run()
    assert [e for e in a['events'] if e['time']<5]==[e for e in b['events'] if e['time']<5]


def test_cached_placement_matches_frozen_interface(placement):
    normal=FrozenPlacement(service=placement.service);f=two_gpu_fleet();c=Cluster(f)
    for i in range(5):
        j=Job(f'j{i}',0,0,'training' if i%2 else 'inference',1,ResourceRequest(2000,10,1)).public()
        assert normal.plans(c,j)==placement.plans(c,j)
        plan=placement.choose(c,j);c.commit(j,plan)
    assert placement.cache_hits>0


def test_cache_exact_on_heterogeneous_multi_gpu_slot_requests(placement):
    f,j,_,_=generate(2207,Condition('multi_heavy','negative','over'),12)
    normal=FrozenPlacement(service=placement.service);c=Cluster(f)
    for job in j:
        public=job.public()
        assert normal.plans(c,public)==placement.plans(c,public)
        assert normal.plans(c,public,empty=True)==placement.plans(c,public,empty=True)
        p=placement.choose(c,public)
        if p is not None:c.commit(public,p)
    for jid in list(c.running):c.release(jid)
    for job in j:assert normal.plans(c,job.public())==placement.plans(c,job.public())


def test_new_namespace_seed_and_common_error_traces(placement):
    assert not set(DEV_SEEDS)&set(TEST_SEEDS)
    f,j,a,c=generate(1103,Condition('high_load','positive','under'),12)
    _,over,_,_=generate(1103,Condition('high_load','positive','over'),12)
    assert [(x.arrival_at,x.ready_at,x.request,x.actual_service) for x in j]==[(x.arrival_at,x.ready_at,x.request,x.actual_service) for x in over]
    aa=StudySimulator(Cluster(f),j,placement,'hrrn_easy',c,a).run()
    bb=StudySimulator(Cluster(f),tuple(reversed(j)),placement,'hrrn_easy',c,a).run()
    assert aa==bb and verify_study(aa,j,f)['success']
