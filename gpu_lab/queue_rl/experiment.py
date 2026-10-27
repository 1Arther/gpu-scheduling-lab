"""Stage-3 artifact lifecycle, common evaluator, and immutable test freeze."""
import argparse
from dataclasses import asdict,replace
from datetime import datetime,timezone
from hashlib import sha256
import json
from pathlib import Path
from time import perf_counter
import numpy as np
from ..queue_sim import Cluster
from ..queue_study import StudySimulator
from ..queue_study.placement import MemoizedPlacement
from ..queue_study.metrics import extended_metrics,flatten
from ..queue_study.audit import verify_study
from .workloads import TRAIN_SEEDS,DEV_SEEDS,TEST_SEEDS,CONDITIONS,record,restore
from .observations import Scales,encode
from .rewards import RewardSpec
from .simulator import DecisionSimulator
from .policies import heuristic,HeuristicSpec,never,fixed,safe_predict
from .audit import verify_rl

ROOT=Path(__file__).resolve().parents[2]
DEFAULT=ROOT/'results/hrrn-stage3-20261026'
BASE_POLICIES=('fifo','sjf','hrrn','hrrn_age','hrrn_easy','hrrn_protect','hrrn_whole_gpu')

def digest(path):
    result=sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):result.update(block)
    return result.hexdigest()
def dump(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('w',encoding='utf-8') as stream:
        json.dump(value,stream,ensure_ascii=False,indent=2,allow_nan=False)
def read(path):return json.loads(Path(path).read_text(encoding='utf-8'))

def preserved_files():
    paths=[]
    for folder in ('gpu_lab/queue_sim','gpu_lab/queue_study','gpu_lab/runtime','models/pod-gpu-rf-v1','results/hrrn-stage2-20261026'):
        paths.extend(p for p in (ROOT/folder).rglob('*') if p.is_file() and '__pycache__' not in str(p))
    paths.extend(ROOT/p for p in ('gpu_lab/resources.py','gpu_lab/profiles.py','tests/test_queue_sim.py','tests/test_queue_study.py','tests/test_resources.py'))
    return {str(p.relative_to(ROOT)).replace('\\','/'):digest(p) for p in sorted(set(paths))}

def prepare(out=DEFAULT):
    out=Path(out);out.mkdir(parents=True,exist_ok=True)
    if (out/'inputs/train.json').exists():raise RuntimeError('Prepared inputs already exist; refusing overwrite')
    dump(out/'preserved-before.json',preserved_files())
    for name,seeds in (('train',TRAIN_SEEDS),('development',DEV_SEEDS),('test',TEST_SEEDS)):
        traces=[record(seed,c) for seed in seeds for c in CONDITIONS]
        dump(out/f'inputs/{name}.json',traces)
    scales=Scales.fit(read(out/'inputs/train.json'));dump(out/'scales.json',asdict(scales))
    protocol=dict(namespace='hrrn-rl-stage3-20261026-v1',quantiles='within-trace quantile, macro mean over conditions and seeds',
        window='trace generator observation_end; complete drain for waiting/slowdown',
        primary_metrics=['p95_wait','long_wait_ratio','completed_work'],
        primary_comparators=['hrrn','hrrn_protect','hrrn_adaptive_heuristic','hrrn_easy'],
        multiplicity='12 primary paired contrasts; Bonferroni simultaneous bootstrap 1-0.05/12 intervals',
        bootstrap='resample 20 paired seed blocks, 100000 replicates, RNG 76291; average conditions within block',
        training=dict(steps_per_model=512,seeds=[17,43],variants=['joint','switch_only','no_hrrn'],idle_weights=[.25,1.]),
        selection='Each variant: minimize dev macro p95 + 8*long_wait_ratio + 0.5*mean_wait; ties lower work loss, then stable ID',
        heuristic_selection='same development objective over predeclared grid',
        fixed_protection=dict(H_abs=12.,kappa=8.),adaptive_eligibility=dict(H_abs=2.,kappa=1.),
        gamma=1.,gae_lambda=1.,reward='negative exact synthetic-time integral through full drain; constant pre-decision prefix charged once',
        training_convergence='Small-budget functional experiment; no claim of convergence',
        future_information='only environment event advancement and offline evaluation access actual synthetic durations',
        test_retuning_forbidden=True)
    dump(out/'protocol.json',protocol)
    return dict(train=24*len(CONDITIONS),development=4*len(CONDITIONS),test=20*len(CONDITIONS),scales=asdict(scales))

def evaluate_trace(trace,policy,placement,scales,model=None,variant='joint',hspec=None,model_id=None):
    f,j,av,cfg=restore(trace);inferences=[];start=perf_counter()
    if policy in BASE_POLICIES:
        config=replace(cfg,protection_absolute_wait=12.,protection_kappa=8.)
        sim=StudySimulator(Cluster(f),j,placement,policy,config,av);r=sim.run()
        audit=verify_study(r,j,f)
    else:
        sim=DecisionSimulator(Cluster(f),j,placement,config=cfg,availability=av,variant=variant)
        def choose(context,mask):
            if policy=='hrrn_adaptive_heuristic':return heuristic(context,mask,hspec)
            if policy=='never':return never(context,mask)
            if policy=='fixed_early':return fixed(context,mask)
            action,reason,ms=safe_predict(model,encode(context,scales,variant),mask)
            inferences.append(ms);return action,reason
        r=sim.run_policy(choose);audit=verify_rl(r,j,f)
    m=flatten(extended_metrics(r,j,f,sim.planning_diagnostics));m['simulation_wall_seconds']=perf_counter()-start
    m['inference_ms_mean']=float(np.mean(inferences)) if inferences else 0.
    m['inference_ms_p95']=float(np.quantile(inferences,.95)) if inferences else 0.
    m['dispatch_total_ms_mean']=(1000*sum(d['wall_seconds'] for d in sim.planning_diagnostics)+sum(getattr(sim,'controller_diagnostics',[])))/max(1,len(sim.planning_diagnostics))
    m['decision_count']=len(getattr(sim,'decisions',[]))
    m['protect_action_ratio']=sum(d['action']>0 for d in getattr(sim,'decisions',[]))/max(1,m['decision_count'])
    creates=[e for e in r['events'] if e['kind']=='protection_create'];rows={x['job_id']:x for x in r['jobs']}
    delays=[rows[e['job_id']]['start_at']-e['time'] for e in creates if rows[e['job_id']]['start_at'] is not None]
    m['activation_mean_observed_delay']=float(np.mean(delays)) if delays else None
    if hasattr(sim,'cost'):m['episode_cost']=sim.cost
    return dict(seed=trace['seed'],condition='__'.join(trace['condition'].values()),policy=policy,model_id=model_id,
        metrics=m,audit=audit,actions=getattr(sim,'decisions',[])),r

def objective(rows):
    def mean(key):return float(np.mean([r['metrics'][key] for r in rows]))
    return mean('p95_wait')+8*mean('long_wait_ratio')+.5*mean('mean_wait')

def freeze(out=DEFAULT):
    out=Path(out)
    if (out/'freeze.json').exists():raise RuntimeError('Already frozen')
    if (out/'test/results.json').exists():raise RuntimeError('Cannot freeze after inspecting formal results')
    selected=read(out/'selection.json')
    metadata=[read(p) for p in (out/'training').glob('*/metadata.json')]
    protocol=read(out/'protocol.json')['training']
    expected={(v,w,s) for v in protocol['variants'] for w in protocol['idle_weights'] for s in protocol['seeds']}
    assert {(m['variant'],m['reward']['idle'],m['seed']) for m in metadata}==expected
    assert all(m['steps']==protocol['steps_per_model'] and m['training_input_sha256']==digest(out/'inputs/train.json') for m in metadata)
    for m in selected['models'].values():assert digest(out/m['path'])==m['sha256']
    paths=list((ROOT/'gpu_lab/queue_rl').glob('*.py'))+[ROOT/'gpu_lab/queue_rl/README.md',ROOT/'tests/test_queue_rl.py',out/'selection.json',out/'protocol.json',out/'scales.json',out/'preserved-before.json']
    paths.extend(p for p in (out/'requirements-lock.txt',out/'environment.json') if p.exists())
    paths.extend(p for p in (out/'pilot-exclusion.json',out/'source-correction-validation.json') if p.exists())
    paths.extend(p for p in (out/'training-reproducibility.json',) if p.exists())
    paths+=list((out/'inputs').glob('*.json'))
    for v in selected['models'].values():paths.extend([out/v['path'],out/v['metadata']])
    dump(out/'freeze.json',dict(files={str(p.relative_to(ROOT)).replace('\\','/'):digest(p) for p in sorted(set(paths))},
        frozen_at_utc=datetime.now(timezone.utc).isoformat(),equal_training_budgets_verified=True,
        preserved_verified=preserved_files()==read(out/'preserved-before.json'),selected=selected,
        timing='Frozen before any formal test evaluation; includes all model, input, metric, simulator and analysis sources'))
    if not read(out/'freeze.json')['preserved_verified']:raise RuntimeError('Frozen previous files changed')

def verify_freeze(out=DEFAULT):
    data=read(Path(out)/'freeze.json')
    for path,want in data['files'].items():
        if digest(ROOT/path)!=want:raise RuntimeError(f'Frozen artifact changed: {path}')
    if preserved_files()!=read(Path(out)/'preserved-before.json'):raise RuntimeError('Previous-stage artifacts changed')
    return True

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('command',choices=['prepare','freeze','verify']);parser.add_argument('--out',type=Path,default=DEFAULT)
    a=parser.parse_args();print({'prepare':prepare,'freeze':freeze,'verify':verify_freeze}[a.command](a.out))
