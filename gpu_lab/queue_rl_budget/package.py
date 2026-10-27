"""Verify actual completion and package only this supplementary study."""
import zipfile
import xml.etree.ElementTree as ET
from .common import ROOT,OUT,OLD,SEEDS,read,dump,digest,now,verify_inputs

def package():
    verify_inputs();a=read(OUT/'analysis/verification.json');gate=read(OUT/'staircase-decision.json')
    assert a['all_audits_passed'] and not any(x['passes'] for x in a['promotions'].values())
    maximum=8192 if gate['stop_before32768'] else 32768
    suffix='-full32768' if maximum==32768 else ''
    for seed in SEEDS:assert read(OUT/f'training/seed-{seed}{suffix}/complete.json')['completed']==maximum
    for p in (OUT/'training').glob('*/metadata-*.json'):
        m=read(p);assert digest(p.with_name(f"model-{m['steps']}.zip"))==m['sha256']
    assert read(OUT/'preservation-verification.json')['all_unchanged']
    if maximum==32768:assert read(OUT/'extension-prefix-verification.json')['all_bit_exact']
    tests=ET.parse(OUT/'unit-tests.xml').getroot();suites=list(tests.iter('testsuite'))
    assert sum(int(x.get('failures',0))+int(x.get('errors',0)) for x in suites)==0
    count=sum(int(x.get('tests',0)) for x in suites)
    for name in ('requirements-lock.txt','environment.json'):
        (OUT/name).write_bytes((OLD/name).read_bytes())
    dump(OUT/'final-test-status.json',dict(time=now(),executed=False,generated=False,final_model_selected=None,
        reason='No budget passes the predeclared useful-learning promotion criteria; retain negative results',
        highest_completed_budget=maximum,training_seeds=SEEDS,unit_tests_passed=count,
        recommendation='Stop the current reinforcement-learning route; improve the verified HRRN queue mechanisms, with explicit protection tradeoffs'))
    paths=list((ROOT/'gpu_lab/queue_rl_budget').glob('*.py'))+[ROOT/'gpu_lab/queue_rl_budget/README.md',ROOT/'tests/test_queue_rl_budget.py']
    paths += [p for p in OUT.rglob('*') if p.is_file() and p.suffix!='.zip' and p.name not in ('release-manifest.json','package-info.json')]
    paths += list((OUT/'training').glob('*/model-*.zip'))
    paths=sorted(set(paths))
    dump(OUT/'release-manifest.json',dict(time=now(),files={str(p.relative_to(ROOT)):digest(p) for p in paths},
        note='Supplementary artifact release hashes, not a newly frozen final-test experiment'))
    paths.append(OUT/'release-manifest.json');dest=OUT/'ppo-budget-code-and-results.zip'
    with zipfile.ZipFile(dest,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for p in paths:z.write(p,p.relative_to(ROOT))
    dump(OUT/'package-info.json',dict(path=str(dest),bytes=dest.stat().st_size,sha256=digest(dest),files=len(paths),
        dependency='Incremental package requires the unchanged original project and stage3 artifacts; no virtualenv included'))
    print(dict(package=str(dest),files=len(paths),unit_tests=count,maximum=maximum),flush=True)

if __name__=='__main__':package()
