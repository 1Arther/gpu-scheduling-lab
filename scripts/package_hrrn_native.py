"""Create an incremental, local delivery archive, excluding large test binaries."""
from pathlib import Path
import hashlib
import json
import zipfile

root=Path(__file__).resolve().parents[1]
out=root/'results/hrrn-native-stage4-20261026'
source=root/'scheduler/volcano-rf'
files=set((root/'k8s/hrrn-stage4').rglob('*'))
files.update((source/'pkg/scheduler/plugins/hrrn').glob('*.go'))
for name in ['pkg/scheduler/plugins/factory.go','pkg/scheduler/framework/session.go','pkg/scheduler/framework/session_plugins.go','pkg/scheduler/framework/job_plan_guard.go','pkg/scheduler/framework/job_plan_guard_test.go']:
    files.add(source/name)
for p in (root/'scripts').glob('*hrrn*'):
    if p.is_file():files.add(p)
files.add(root/'tests/test_hrrn_stage4_artifacts.py')
for p in out.rglob('*'):
    if p.is_file() and 'bin' not in p.relative_to(out).parts and p.suffix not in {'.zip','.pid'} and p.name not in {'delivery-manifest.json','delivery-zip-sha.json'}:
        files.add(p)
files.add(out/'bin/vc-scheduler-hrrn')
files=sorted(p for p in files if p.is_file() and '__pycache__' not in p.parts)
manifest={str(p.relative_to(root)).replace('\\','/'):{'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in files}
(out/'delivery-manifest.json').write_text(json.dumps({'kind':'incremental; requires existing frozen project/model','files':manifest},indent=2,ensure_ascii=False),encoding='utf-8')
files.append(out/'delivery-manifest.json')
target=out/'hrrn-native-stage4-incremental.zip'
with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
    for p in files:z.write(p,p.relative_to(root).as_posix())
with zipfile.ZipFile(target) as z:assert z.testzip() is None
record={'path':str(target),'bytes':target.stat().st_size,'sha256':hashlib.sha256(target.read_bytes()).hexdigest(),'file_count':len(files)}
(out/'delivery-zip-sha.json').write_text(json.dumps(record,indent=2,ensure_ascii=False),encoding='utf-8')
print(json.dumps(record,ensure_ascii=False))
