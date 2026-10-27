"""Read-only frozen-base overlay for attribution of existing native test failures."""
import json
import subprocess
from pathlib import Path
root=Path(__file__).resolve().parents[1]
src=root/'scheduler/volcano-rf'
out=root/'results/hrrn-native-stage4-20261026/baseline-overlay'
out.mkdir(exist_ok=True)
base='1a5caec'
replacements={}
for name in ['pkg/scheduler/framework/session.go','pkg/scheduler/framework/session_plugins.go','pkg/scheduler/plugins/factory.go']:
    data=subprocess.check_output(['git','show',base+':'+name],cwd=src)
    target=out/Path(name).name;target.write_bytes(data)
    replacements[str(src/name)]=str(target)
for name in ['pkg/scheduler/framework/job_plan_guard.go','pkg/scheduler/framework/job_plan_guard_test.go']:
    replacements[str(src/name)]=''
(out/'overlay.json').write_text(json.dumps({'Replace':replacements},indent=2),encoding='utf-8')
print(out/'overlay.json')
