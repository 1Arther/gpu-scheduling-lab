"""Functional mechanism verification. Never trains a chip-awareness model."""
from copy import deepcopy
from dataclasses import replace
import json
import random
import pytest
from gpu_lab.resources import GPUState, ResourceRequest
from gpu_lab.queue_sim import Job, Config, Cluster, FrozenPlacement, Simulator, PublicJob, AvailabilityEvent
from gpu_lab.queue_sim.placement import Plan, free
from gpu_lab.queue_sim.policies import Protection, POLICIES, order, response_ratio
from gpu_lab.queue_sim.scenarios import blocking_case, cancellation_case, invalid_request_case, invalidation_case, seeded_smoke, two_gpu_fleet
from gpu_lab.queue_sim.audit import verify_result
from gpu_lab.queue_sim.cli import frozen_hashes, DEFAULT_BUNDLE, input_record, load_input


@pytest.fixture(scope='module')
def placement():
    before=frozen_hashes(DEFAULT_BUNDLE)
    p=FrozenPlacement()
    yield p
    assert before==frozen_hashes(DEFAULT_BUNDLE)


def run_case(factory,placement,policy='hrrn_protect',config=None):
    fleet,jobs,availability=factory()
    result=Simulator(Cluster(fleet),jobs,placement,policy,config,availability).run()
    assert verify_result(result,jobs,fleet)['success']
    return result


def test_sorting_and_public_observation():
    req=ResourceRequest(10,10)
    js=[Job('a',0,0,'training',10,req,actual_service=999),
        Job('b',0,2,'inference',1,req,actual_service=777),
        Job('c',0,1,'training',4,req,actual_service=222)]
    pub=[j.public() for j in js];conf=Config()
    assert all(not hasattr(j,'actual_service') and not hasattr(j,'cancel_at') for j in pub)
    assert response_ratio(pub[1],4,conf)==3.
    expected=dict(fifo=['a','c','b'],sjf=['b','c','a'],hrrn=['b','c','a'],hrrn_age=['a','c','b'])
    for name,ids in expected.items(): assert [j.job_id for j in order(pub,4,name,conf)]==ids


@pytest.mark.parametrize('change',[dict(actual_service=0),dict(estimated_service=float('nan')),
    dict(ready_at=-1),dict(gpu_count=0),dict(cancel_at=-1),dict(task_type='other')])
def test_invalid_input_is_rejected(change):
    with pytest.raises(ValueError): Job(**dict(dict(job_id='j',arrival_at=0,ready_at=0,task_type='training',
        estimated_service=1,request=ResourceRequest(10,10)),**change))


@pytest.mark.parametrize('policy',POLICIES)
def test_minimal_blocking_case_and_full_release(placement,policy):
    result=run_case(blocking_case,placement,policy)
    rows={r['job_id']:r for r in result['jobs']}
    protected=policy in ('hrrn_protect','hrrn_whole_gpu')
    assert rows['10-large-two-gpu']['start_at']==(2. if protected else 10.)
    assert len(rows['10-large-two-gpu']['gpu_ids'])==2
    assert result['metrics']['completed_total']==12
    assert all(not j['allocation_active'] and j['released_at']==j['finish_at'] for j in result['jobs'])
    assert result['metrics']['all_resources_released'] and result['metrics']['final_protection_cleared']
    if policy=='hrrn_protect': assert rows['30-spare-capacity']['start_at']==1.1
    if policy=='hrrn_whole_gpu': assert rows['30-spare-capacity']['start_at']==2.


def test_failed_multi_gpu_commit_has_no_partial_side_effect(placement):
    fleet=two_gpu_fleet();cluster=Cluster(fleet);cap=next(iter(fleet.values())).profile.modeled_memory_mib
    bg=Job('bg',0,0,'training',1,ResourceRequest(cap*6//10,60)).public()
    key=sorted(fleet)[1];cluster.commit(bg,cluster.fixed_plan(bg,(key,)))
    large=replace(bg,job_id='large',gpu_count=2)
    ids=tuple(sorted(fleet));plan=Plan(cluster.nodes[ids[0]],ids,0.,tuple((k,cluster.gpus[k].revision) for k in ids))
    before=cluster.snapshot();running=deepcopy(cluster.running)
    assert placement.choose(cluster,large) is None
    with pytest.raises(ValueError):cluster.commit(large,plan)
    assert cluster.snapshot()==before and cluster.running==running
    bad=replace(plan,gpu_ids=(ids[0],ids[0]))
    with pytest.raises(ValueError):cluster.commit(large,bad)
    assert cluster.snapshot()==before


def test_stale_plan_rejected_atomically(placement):
    fleet=two_gpu_fleet();cluster=Cluster(fleet);j=Job('j',0,0,'training',1,ResourceRequest(10,10)).public()
    plan=placement.choose(cluster,j);other=replace(j,job_id='other')
    cluster.commit(other,cluster.fixed_plan(other,plan.gpu_ids));before=cluster.snapshot()
    with pytest.raises(ValueError,match='Stale'):cluster.commit(j,plan)
    assert cluster.snapshot()==before


def test_no_cross_node_partial_fit_or_permanent_invalid_protection(placement):
    fleet=two_gpu_fleet();nodes={k:f'node-{i}' for i,k in enumerate(fleet)}
    job=Job('two',0,0,'training',1,ResourceRequest(10,10),gpu_count=2)
    r=Simulator(Cluster(fleet,nodes),(job,),placement,'hrrn_protect').run()
    assert r['jobs'][0]['status']=='rejected'
    assert r['metrics']['protection_epochs']==0
    invalid=run_case(invalid_request_case,placement)
    assert invalid['metrics']['rejected_count']==1 and invalid['metrics']['completed_total']==1
    assert not any(e['kind']=='protection_create' and e['job_id']=='impossible' for e in invalid['events'])


def test_slots_and_all_three_protection_dimensions(placement):
    fleet=two_gpu_fleet();cluster=Cluster(fleet);cap=next(iter(fleet.values())).profile.modeled_memory_mib
    owner=Job('owner',0,0,'training',1,ResourceRequest(cap//3,30,6)).public()
    plan=placement.choose(cluster,owner,empty=True);p=Protection.create(1,owner,plan,cluster,0.)
    request=Job('slots',0,0,'inference',1,ResourceRequest(10,10,5)).public()
    assert not p.allows(cluster,request,plan.gpu_ids)
    request=replace(request,request=ResourceRequest(10,10,4));before=p.idle(cluster)
    cluster.commit(request,cluster.fixed_plan(request,plan.gpu_ids,protection=p),p)
    assert p.idle(cluster)==before
    assert all(v>=0 for k in fleet for v in free(cluster.gpus[k]))


def test_stale_protection_epoch_rejected_without_mutation(placement):
    cluster=Cluster(two_gpu_fleet())
    owner=Job('owner',0,0,'training',1,ResourceRequest(10,10)).public()
    capacity_plan=placement.choose(cluster,owner,empty=True)
    first=Protection.create(1,owner,capacity_plan,cluster,0.)
    later=replace(owner,job_id='later')
    plan=placement.choose(cluster,later,first)
    second=replace(first,epoch=2)
    before=cluster.snapshot()
    with pytest.raises(ValueError,match='Stale protection'):
        cluster.commit(later,plan,second)
    assert cluster.snapshot()==before and not cluster.running
    with pytest.raises(ValueError,match='Stale protection'):
        cluster.commit(later,plan,None)


def test_capacity_query_and_guard_do_not_mutate_or_mask_rf_state(placement):
    fleet=two_gpu_fleet();cluster=Cluster(fleet);cap=next(iter(fleet.values())).profile.modeled_memory_mib
    job=Job('owner',0,0,'training',1,ResourceRequest(cap*6//10,60)).public()
    before=cluster.snapshot();plan=placement.choose(cluster,job,empty=True)
    assert cluster.snapshot()==before
    p=Protection.create(1,job,plan,cluster,0.)
    backfill=replace(job,job_id='later')
    normal=placement.plans(cluster,backfill);limited=placement.plans(cluster,backfill,p)
    assert normal[0].gpu_ids==plan.gpu_ids
    assert limited and all(k not in plan.gpu_ids for k in limited[0].gpu_ids)
    assert limited[0].score==next(q.score for q in normal if q.gpu_ids==limited[0].gpu_ids)
    assert cluster.snapshot()==before


def test_target_is_fixed_even_if_another_combination_becomes_free(placement):
    fleet,jobs,_=blocking_case(3);sample=next(iter(fleet.values()))
    extra='mechanism-node/gpu-2';fleet[extra]=GPUState(extra,sample.profile)
    availability=(AvailabilityEvent(0,extra,False),AvailabilityEvent(.75,extra,True))
    result=Simulator(Cluster(fleet),jobs,placement,'hrrn_protect',availability=availability).run()
    assert verify_result(result,jobs,fleet)['success']
    created=next(e for e in result['events'] if e['kind']=='protection_create' and e['job_id']=='10-large-two-gpu')
    assert extra not in created['protection']['targets']
    started=next(e for e in result['events'] if e['kind']=='start' and e['job_id']=='10-large-two-gpu')
    assert started['time']==2. and list(started['plan']['gpu_ids'])==created['protection']['targets']


def test_threshold_timer_fires_without_arrival_or_completion(placement):
    r=run_case(blocking_case,placement)
    p=next(e for e in r['events'] if e['kind']=='protection_create')
    assert p['time']==.5


def test_cancel_protection_restores_backfill(placement):
    r=run_case(cancellation_case,placement)
    assert any(e['kind']=='protection_clear' and e['reason']=='owner_cancelled' for e in r['events'])
    row={j['job_id']:j for j in r['jobs']}
    assert row['10-large-two-gpu']['status']=='cancelled' and row['10-large-two-gpu']['start_at'] is None
    assert row['20-small-01']['start_at']==1.
    assert r['metrics']['censored_wait_count']==1 and r['metrics']['mean_wait'] is None


def test_cancel_running_job_releases_all_gpus_once(placement):
    fleet=two_gpu_fleet();cap=next(iter(fleet.values())).profile.modeled_memory_mib
    req=ResourceRequest(cap*6//10,60)
    jobs=(Job('a',0,0,'training',2,req,gpu_count=2,actual_service=10,cancel_at=1),
          Job('b',.5,.5,'inference',1,req,actual_service=1))
    r=Simulator(Cluster(fleet),jobs,placement,'hrrn_protect',Config(protection_absolute_wait=10)).run()
    assert verify_result(r,jobs,fleet)['success']
    assert r['metrics']['cancelled_count']==1 and r['metrics']['completed_total']==1
    assert next(j for j in r['jobs'] if j['job_id']=='b')['start_at']==1.
    assert not any(e['kind']=='complete' and e['job_id']=='a' for e in r['events'])


def test_invalidation_clears_then_recreates_protection(placement):
    r=run_case(invalidation_case,placement)
    clear=next(e for e in r['events'] if e['kind']=='protection_clear' and e['reason']=='target_unavailable_or_invalid')
    assert clear['time']==.75
    assert any(e['kind']=='protection_create' and e['time']>=3. for e in r['events'])
    assert r['metrics']['completed_total']==len(r['jobs'])


def test_unavailable_job_is_censored_without_infinite_resource_protection(placement):
    fleet=two_gpu_fleet();req=ResourceRequest(10,10)
    jobs=(Job('j',0,0,'training',1,req,gpu_count=2),)
    availability=tuple(AvailabilityEvent(0,k,False) for k in fleet)
    r=Simulator(Cluster(fleet),jobs,placement,'hrrn_protect',availability=availability).run()
    assert verify_result(r,jobs,fleet)['success']
    assert r['metrics']['pending_count']==1 and r['metrics']['mean_wait'] is None
    assert r['metrics']['long_wait_ratio']==1. and r['metrics']['protection_epochs']==0


def test_ready_time_not_arrival_time_controls_wait(placement):
    fleet=two_gpu_fleet();j=Job('j',0,5,'inference',1,ResourceRequest(10,10))
    r=Simulator(Cluster(fleet),(j,),placement,'hrrn').run()
    assert r['jobs'][0]['start_at']==5. and r['jobs'][0]['wait']==0.


@pytest.mark.parametrize('seed',[7,31,20261023])
def test_random_resource_invariants_and_seed_reproducibility(placement,seed):
    factory=lambda:seeded_smoke(seed,12)
    first=run_case(factory,placement)
    second=run_case(factory,placement)
    assert json.dumps(first,sort_keys=True)==json.dumps(second,sort_keys=True)
    fleet,jobs,av=factory()
    reordered=Simulator(Cluster(fleet),tuple(reversed(jobs)),placement,'hrrn_protect').run()
    assert first==reordered
    assert first['metrics']['completed_total']==12
    assert all(0<=x<=1 for x in first['metrics']['logical_reserved_rate'].values())


def test_policy_sees_no_true_duration_and_never_trains(placement,monkeypatch):
    from sklearn.ensemble import RandomForestClassifier
    def forbidden(*args,**kwargs):raise AssertionError('RF training is forbidden in queue MVP')
    monkeypatch.setattr(RandomForestClassifier,'fit',forbidden)
    original=placement.choose;seen=[]
    def checked(cluster,job,*args,**kwargs):
        assert isinstance(job,PublicJob) and not isinstance(job,Job)
        assert not hasattr(job,'actual_service');seen.append(job.job_id)
        return original(cluster,job,*args,**kwargs)
    monkeypatch.setattr(placement,'choose',checked)
    run_case(blocking_case,placement)
    assert seen


def test_independent_audit_rejects_resource_tampering(placement):
    fleet,jobs,_=blocking_case();r=Simulator(Cluster(fleet),jobs,placement,'hrrn_protect').run()
    damaged=deepcopy(r)
    e=next(e for e in damaged['events'] if e['kind']=='start')
    k=next(iter(e['state']));e['state'][k]['remaining_compute_units']=-1
    with pytest.raises(AssertionError):verify_result(damaged,jobs,fleet)


def test_saved_synthetic_input_roundtrip(placement,tmp_path):
    fleet,jobs,av=seeded_smoke(31,5);path=tmp_path/'trace.json'
    path.write_text(json.dumps(input_record(fleet,jobs,av,Config())),encoding='utf-8')
    ff,jj,aa,conf,nodes=load_input(path)
    assert jj==jobs and aa==av
    a=Simulator(Cluster(fleet),jobs,placement,'hrrn',Config()).run()
    b=Simulator(Cluster(ff,nodes),jj,placement,'hrrn',conf,aa).run()
    assert a==b
