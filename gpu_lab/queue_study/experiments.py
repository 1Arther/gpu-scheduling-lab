"""Develop -> seal inputs/evaluator -> execute holdout, never tune on test."""
from concurrent.futures import ProcessPoolExecutor,as_completed
from dataclasses import asdict,replace
from hashlib import sha256
from pathlib import Path
import csv,gzip,json,time
import numpy as np
from ..profiles import ROOT
from ..queue_sim import Cluster,FrozenPlacement,Config
from ..queue_sim.cli import input_record,load_input,frozen_hashes,DEFAULT_BUNDLE
from .simulator import StudySimulator,POLICIES
from .placement import MemoizedPlacement
from .metrics import extended_metrics,flatten
from .audit import verify_study
from .workloads import generate,DEV_SEEDS,TEST_SEEDS,DEV_CONDITIONS,TEST_CONDITIONS,NAMESPACE

GRID=tuple((h,k) for h in (.5,2.,6.,12.) for k in (.5,2.,8.))
SERVICE=None


def write_json(path,value):
    Path(path).write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')


def write_csv(path,rows):
    keys=list(dict.fromkeys(k for r in rows for k in r))
    with Path(path).open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=keys);w.writeheader();w.writerows(rows)


def initialize():
    global SERVICE
    SERVICE=FrozenPlacement().service


def run_one(fleet,jobs,availability,config,policy):
    placement=MemoizedPlacement(service=SERVICE)
    sim=StudySimulator(Cluster(fleet),jobs,placement,policy,config,availability)
    result=sim.run();audit=verify_study(result,jobs,fleet)
    m=extended_metrics(result,jobs,fleet,sim.planning_diagnostics)
    m['placement_cache_hits']=placement.cache_hits
    digest=sha256(json.dumps(result,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
    return result,flatten(m),audit,digest,sim.planning_diagnostics


def dev_task(task):
    seed,condition,path=task
    fleet,jobs,av,config=generate(seed,condition)
    write_json(Path(path)/f'{seed}-{condition.name}-input.json',input_record(fleet,jobs,av,config))
    rows=[]
    for policy in ('hrrn','hrrn_easy'):
        _,m,a,d,_=run_one(fleet,jobs,av,config,policy)
        rows.append(dict(seed=seed,condition=condition.name,scenario=condition.scenario,policy=policy,H_abs=None,kappa=None,**m))
    for h,k in GRID:
        conf=replace(config,protection_absolute_wait=h,protection_kappa=k)
        _,m,a,d,_=run_one(fleet,jobs,av,conf,'hrrn_protect')
        rows.append(dict(seed=seed,condition=condition.name,scenario=condition.scenario,policy='hrrn_protect',H_abs=h,kappa=k,**m))
    return rows


def pool_tasks(function,tasks,workers,label):
    rows=[];started=time.perf_counter()
    with ProcessPoolExecutor(max_workers=workers,initializer=initialize) as pool:
        futures=[pool.submit(function,t) for t in tasks]
        for i,f in enumerate(as_completed(futures),1):
            rows.extend(f.result())
            if i%8==0 or i==len(tasks):
                print(f'{label}: {i}/{len(tasks)} trace blocks, elapsed={time.perf_counter()-started:.1f}s',flush=True)
    return rows


def develop(out,workers=4):
    out=Path(out);out.mkdir(parents=True,exist_ok=False);(out/'inputs').mkdir()
    protocol=dict(namespace=NAMESPACE,dev_seeds=DEV_SEEDS,test_seeds=TEST_SEEDS,grid=GRID,
        selection='Guard mean slowdown <=1.05*HRRN; work >=.98*HRRN; small P95 <=1.1*HRRN+.25. Among feasible minimize P95/(HRRN P95+.1)+long fraction/(HRRN long fraction+.02). If none feasible: minimize summed normalized guard violations, then objective; report failure.',
        evaluator='mean of per-trace metrics; equal condition weight within development; holdout equal scenario weight',
        long_wait_threshold=8.,tau_bounded_slowdown=1.,synthetic_only=True)
    write_json(out/'protocol-before-development.json',protocol)
    rows=pool_tasks(dev_task,[(s,c,str(out/'inputs')) for s in DEV_SEEDS for c in DEV_CONDITIONS],workers,'development')
    rows.sort(key=lambda r:(r['seed'],r['condition'],r['policy'],r['H_abs'] or 0,r['kappa'] or 0))
    write_csv(out/'runs.csv',rows)
    base=[r for r in rows if r['policy']=='hrrn']
    keys=('p95_wait','mean_wait','long_wait_ratio','completed_work','mean_bounded_slowdown','small_p95_wait')
    avg=lambda rr,key:float(np.mean([r[key] for r in rr if r[key] is not None]))
    baseline={key:avg(base,key) for key in keys};table=[]
    for h,k in GRID:
        rr=[r for r in rows if r['H_abs']==h and r['kappa']==k]
        m={key:avg(rr,key) for key in keys}
        violations=(max(0,m['mean_bounded_slowdown']/(1.05*baseline['mean_bounded_slowdown'])-1),
                    max(0,1-m['completed_work']/(.98*baseline['completed_work'])),
                    max(0,m['small_p95_wait']/(1.1*baseline['small_p95_wait']+.25)-1))
        objective=m['p95_wait']/(baseline['p95_wait']+.1)+m['long_wait_ratio']/(baseline['long_wait_ratio']+.02)
        table.append(dict(H_abs=h,kappa=k,guard_violation=sum(violations),eligible=sum(violations)<1e-12,
                          objective=objective,**m))
    chosen=min(table,key=lambda r:(not r['eligible'],r['guard_violation'],r['objective'],-r['H_abs'],-r['kappa']))
    write_csv(out/'parameter-sensitivity.csv',table)
    write_json(out/'selection.json',dict(selected=chosen,baseline=baseline,selection_passed_guards=chosen['eligible'],
        warning=None if chosen['eligible'] else 'No candidate passed development safeguards; holdout is a diagnostic of the least-violating candidate, not a validated improvement.',
        H_abs=chosen['H_abs'],kappa=chosen['kappa'],test_used=False))
    print('DEVELOPMENT SELECTION',json.dumps(chosen),flush=True)
    return chosen


def evaluation_sources():
    files=list((ROOT/'gpu_lab/queue_study').glob('*.py'))
    files+=list((ROOT/'gpu_lab/queue_sim').glob('*.py'))
    files += [ROOT/'tests/test_queue_study.py',ROOT/'tests/test_queue_sim.py']
    return {p.relative_to(ROOT).as_posix():sha256(p.read_bytes()).hexdigest() for p in sorted(files)}


def freeze(out,development):
    out=Path(out);out.mkdir(parents=True,exist_ok=False);(out/'inputs').mkdir()
    selection=json.loads((Path(development)/'selection.json').read_text(encoding='utf-8'))
    traces=[]
    for seed in TEST_SEEDS:
        for condition in TEST_CONDITIONS:
            fleet,jobs,av,config=generate(seed,condition)
            config=replace(config,protection_absolute_wait=selection['H_abs'],protection_kappa=selection['kappa'])
            name=f'{seed}-{condition.name}-input.json';path=out/'inputs'/name
            write_json(path,input_record(fleet,jobs,av,config))
            traces.append(dict(seed=seed,condition=condition.name,scenario=condition.scenario,correlation=condition.correlation,
                error=condition.error,input=name,sha256=sha256(path.read_bytes()).hexdigest()))
    lock=dict(namespace=NAMESPACE,stage='sealed_before_holdout_execution',policies=POLICIES,
        development_selection=selection,selection_sha256=sha256((Path(development)/'selection.json').read_bytes()).hexdigest(),
        dev_seeds=DEV_SEEDS,test_seeds=TEST_SEEDS,conditions=[asdict(c) for c in TEST_CONDITIONS],
        traces=traces,evaluation_sources_sha256=evaluation_sources(),frozen_backend_sha256=frozen_hashes(DEFAULT_BUNDLE),
        aggregation='equal scenario weights; average conditions within scenario; paired complete seed blocks',
        confidence='paired 20-block bootstrap, 95% exploratory; 9 primary contrasts also Bonferroni-adjusted intervals',
        primary_metrics=['p95_wait','long_wait_ratio','completed_work'],primary_comparators=['hrrn','hrrn_easy','hrrn_whole_gpu'],
        detailed_event_seed=TEST_SEEDS[0],synthetic_only=True,job_count_per_trace=24)
    write_json(out/'freeze.json',lock)
    print('SEALED',len(traces),'independent input traces; source/evaluator/model checksums frozen',flush=True)


def check_freeze(out):
    out=Path(out);lock=json.loads((out/'freeze.json').read_text(encoding='utf-8'))
    assert evaluation_sources()==lock['evaluation_sources_sha256'],'Evaluator/source changed since freeze'
    assert frozen_hashes(DEFAULT_BUNDLE)==lock['frozen_backend_sha256'],'Frozen backend changed'
    for r in lock['traces']:
        assert sha256((out/'inputs'/r['input']).read_bytes()).hexdigest()==r['sha256'],r['input']
    return lock


def test_task(task):
    trace,out=task;out=Path(out)
    fleet,jobs,av,config,nodes=load_input(out/'inputs'/trace['input'])
    assert all(k.rsplit('/gpu-',1)[0]==v for k,v in nodes.items())
    rows=[];records=[]
    offset=(trace['seed']+sum(trace['condition'].encode()))%len(POLICIES)
    policies=POLICIES[offset:]+POLICIES[:offset]
    for policy in policies:
        result,m,a,d,diag=run_one(fleet,jobs,av,config,policy)
        row=dict(seed=trace['seed'],condition=trace['condition'],scenario=trace['scenario'],correlation=trace['correlation'],
                 error=trace['error'],policy=policy,input_sha256=trace['sha256'],result_sha256=d,
                 audit_events=a['events_checked'],audit_starts=a['atomic_starts'],audit_backfills=a['easy_backfills_checked'],**m)
        rows.append(row);records.append(dict(policy=policy,metrics=m,jobs=result['jobs'],audit=a,result_sha256=d))
        if trace['seed']==TEST_SEEDS[0]:
            with gzip.open(out/'details'/f"{trace['condition']}-{policy}.json.gz",'wt',encoding='utf-8') as f:json.dump(result,f,ensure_ascii=False,allow_nan=False)
    with gzip.open(out/'records'/f"{trace['seed']}-{trace['condition']}.json.gz",'wt',encoding='utf-8') as f:json.dump(records,f,ensure_ascii=False,allow_nan=False)
    return rows


def test(out,workers=4):
    out=Path(out);lock=check_freeze(out)
    for name in ('records','details'):(out/name).mkdir(exist_ok=False)
    rows=pool_tasks(test_task,[(r,str(out)) for r in lock['traces']],workers,'holdout')
    rows.sort(key=lambda r:(r['seed'],r['condition'],r['policy']))
    write_csv(out/'runs.csv',rows)
    check_freeze(out)
    write_json(out/'verification.json',dict(success=True,runs=len(rows),input_traces=len(lock['traces']),seed_blocks=len(TEST_SEEDS),
        events_checked=sum(r['audit_events'] for r in rows),full_job_starts=sum(r['audit_starts'] for r in rows),
        easy_backfills_checked=sum(r['audit_backfills'] for r in rows),source_and_inputs_unchanged=True,
        completed_all_jobs=all(r['completed_total']==24 and r['pending_count']==0 for r in rows)))


def replay(out,seed,condition,policy):
    out=Path(out);lock=check_freeze(out)
    trace=next(r for r in lock['traces'] if r['seed']==seed and r['condition']==condition)
    fleet,jobs,av,config,nodes=load_input(out/'inputs'/trace['input']);initialize()
    result,m,a,d,diag=run_one(fleet,jobs,av,config,policy)
    with gzip.open(out/'records'/f'{seed}-{condition}.json.gz','rt',encoding='utf-8') as f:record=next(r for r in json.load(f) if r['policy']==policy)
    assert d==record['result_sha256'],'Deterministic replay changed'
    print(json.dumps(dict(replay_identical=True,result_sha256=d,audit=a)),flush=True)


def profile(development):
    """Serial CPU measurements, separated from parallel holdout timings."""
    initialize();rows=[]
    conditions=[DEV_CONDITIONS[i] for i in (0,1,6,7)]
    selection=json.loads((Path(development)/'selection.json').read_text(encoding='utf-8'))
    for condition in conditions:
        f,j,a,c=generate(DEV_SEEDS[0],condition)
        c=replace(c,protection_absolute_wait=selection['H_abs'],protection_kappa=selection['kappa'])
        for repeat in range(4):
            offset=repeat%len(POLICIES)
            for policy in POLICIES[offset:]+POLICIES[:offset]:
                _,m,_,_,_=run_one(f,j,a,c,policy)
                if repeat:rows.append(dict(condition=condition.name,repeat=repeat,policy=policy,
                    decision_ms_mean=m['decision_ms_mean'],decision_ms_p95=m['decision_ms_p95'],placement_calls=m['placement_calls'],
                    placement_cache_hits=m['placement_cache_hits']))
    write_csv(Path(development)/'serial-profile.csv',rows)
    print('Serial instrumented CPU profiling complete:',len(rows),'measurements',flush=True)
