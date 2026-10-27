"""Summarize actual native test artifacts; do not suppress failed/skip records."""
import csv
import difflib
import hashlib
import json
import re
import subprocess
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'results/hrrn-native-stage4-20261026'
SRC=ROOT/'scheduler/volcano-rf'
def events(path):
    return [json.loads(line) for line in path.read_text(encoding='utf-8-sig').splitlines() if line.strip()]
def dump(name,obj):
    (OUT/name).write_text(json.dumps(obj,indent=2,ensure_ascii=False),encoding='utf-8')
def summary(path):
    e=events(path)
    names={r['Test'] for r in e if 'Test' in r}
    leaves={n for n in names if not any(x.startswith(n+'/') for x in names)}
    return {'file':path.name,'top_level_passes':sum(r.get('Action')=='pass' and 'Test' in r and '/' not in r['Test'] for r in e),
        'leaf_passes':sum(r.get('Action')=='pass' and r.get('Test') in leaves for r in e),
        'leaf_failures':sum(r.get('Action')=='fail' and r.get('Test') in leaves for r in e),
        'leaf_skips':sum(r.get('Action')=='skip' and r.get('Test') in leaves for r in e),
        'package_status':e[-1].get('Action'),
        'failed_tests':sorted({r['Test'] for r in e if r.get('Action')=='fail' and 'Test' in r})}

packages=['plugins-hrrn','framework','plugins-priority','plugins-gang','plugins-drf','plugins-proportion','plugins-deviceshare','plugins-predicates','actions-allocate','actions-enqueue','actions-backfill','api-devices-nvidia-vgpu']
records=[summary(OUT/(p+'-tests.jsonl')) for p in packages]
dump('test-summary.json',records)
with (OUT/'test-summary.csv').open('w',newline='',encoding='utf-8') as f:
    w=csv.DictWriter(f,fieldnames=list(records[0]));w.writeheader();w.writerows(records)
extra={n:summary(OUT/n) for n in ['allocate-baseline-full.jsonl','allocate-baseline-repeat.jsonl','allocate-baseline-pvc-repeat.jsonl','allocate-pvc-isolated.jsonl','actions-allocate-isolated-tests.jsonl','allocate-current-repeat.jsonl','hrrn-repeat.jsonl'] if (OUT/n).exists()}
dump('diagnostic-test-summary.json',extra)
before=json.loads((OUT/'frozen-before.json').read_text(encoding='utf-8-sig'))
after={p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in before}
changed=[p for p in before if before[p]!=after[p]]
dump('frozen-after.json',after)
dump('frozen-verification.json',{'files':len(before),'changed':changed,'unchanged':not changed})
assert not changed,changed
logs=''.join(e.get('Output','') for e in events(OUT/'plugins-hrrn-tests.jsonl'))
allocations=[l for l in logs.splitlines() if '"rf-job UUID allocation"' in l]
fallbacks=[l for l in allocations if 'fallback=""' not in l]
assert not fallbacks,'HRRN live RF integration silently fell back'
dump('hrrn-rf-evidence.json',{'native_uuid_allocation_log_records':len(allocations),'nonempty_fallback_records':len(fallbacks),'frozen_model_sha':before['models\\pod-gpu-rf-v1\\random_forest.joblib']})
for name in ['native-comparisons','bypass-comparisons','placement-comparisons']:
    rows=json.loads((OUT/(name+'.json')).read_text())
    rows=[{k:v for k,v in r.items() if k!='tiers'} for r in rows]
    with (OUT/(name+'.csv')).open('w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
patch=subprocess.check_output(['git','diff','--binary'],cwd=SRC)
new_files=subprocess.check_output(['git','ls-files','--others','--exclude-standard'],cwd=SRC).decode().splitlines()
for name in new_files:
    if not name.endswith('.go'):continue
    lines=(SRC/name).read_text(encoding='utf-8').splitlines(keepends=True)
    patch+=('diff --git a/'+name+' b/'+name+'\nnew file mode 100644\n'+''.join(difflib.unified_diff([],lines,fromfile='/dev/null',tofile='b/'+name))).encode()
(OUT/'source.patch').write_bytes(patch)
dump('source-provenance.json',{'base_commit':subprocess.check_output(['git','rev-parse','1a5caec'],cwd=SRC).decode().strip(),
    'branch':subprocess.check_output(['git','branch','--show-current'],cwd=SRC).decode().strip(),
    'patch_sha256':hashlib.sha256(patch).hexdigest(),'cluster_executed':False,'images_built':False,
    'execution':'Windows Go cross compile; actual Linux/amd64 Go test binaries executed inside local WSL; fake Kubernetes/Volcano clients; frozen Python RF on CPU'})
print(json.dumps({'core':records[0],'rf':records[-1],'preserved_files':len(before),'baseline_pvc':extra.get('allocate-baseline-pvc-repeat.jsonl'),'rf_allocations_without_fallback':len(allocations)},ensure_ascii=False,indent=2))
