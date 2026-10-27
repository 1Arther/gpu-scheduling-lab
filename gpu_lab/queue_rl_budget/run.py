"""Reproduce in a new output directory. Never overwrite the original stage 3."""
import os
import subprocess
import sys
from .common import OUT,ROOT,SEEDS,read,dump,now,prepare,verify_inputs,preserved,verify_freeze

def command(module,*args):
    subprocess.run([sys.executable,'-m',f'gpu_lab.queue_rl_budget.{module}',*map(str,args)],cwd=ROOT,check=True)

def train_parallel(maximum):
    running=[]
    for seed in SEEDS:
        log=(OUT/f'train-{seed}-{maximum}.log').open('w',encoding='utf-8')
        p=subprocess.Popen([sys.executable,'-m','gpu_lab.queue_rl_budget.train','--seed',str(seed),'--maximum',str(maximum)],cwd=ROOT,stdout=log,stderr=subprocess.STDOUT)
        running.append((p,log))
    codes=[]
    for p,f in running:codes.append(p.wait());f.close()
    if any(codes):raise RuntimeError(f'Training failed: {codes}; see per-seed logs')

def verify_preservation():
    verify_inputs();verify_freeze()
    original=read(OUT/'preserved-before.json');current=preserved()
    assert current==original,'Original evidence changed'
    dump(OUT/'preservation-verification.json',dict(time=now(),all_unchanged=True,files=len(current),original_freeze_verified=True))

def run():
    os.environ['OMP_NUM_THREADS']='1';os.environ['OPENBLAS_NUM_THREADS']='1'
    prepare();command('diagnose');command('accumulation');command('enrich');command('counterfactual')
    train_parallel(8192);command('evaluate')
    if not read(OUT/'staircase-decision.json')['stop_before32768']:
        # Fresh exact-prefix run avoids silently resetting environment/RNG on resume.
        # Its repeated prefix is included in the actual cost accounting.
        train_parallel(32768);command('evaluate','--extended');command('verify_extensions')
    command('analyze');command('checkpoints');verify_preservation();command('report')
    os.environ['PYTEST_DISABLE_PLUGIN_AUTOLOAD']='1'
    with (OUT/'unit-tests.log').open('w',encoding='utf-8') as f:
        subprocess.run([sys.executable,'-m','pytest','-q','tests/test_queue_rl_budget.py','tests/test_queue_rl.py',
            'tests/test_queue_sim.py','tests/test_queue_study.py','tests/test_resources.py',f'--junitxml={OUT / "unit-tests.xml"}'],cwd=ROOT,stdout=f,stderr=subprocess.STDOUT,check=True)
    command('package')
    print(f'Completed: {OUT}',flush=True)

if __name__=='__main__':run()
