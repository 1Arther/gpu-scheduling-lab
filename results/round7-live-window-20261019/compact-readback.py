import json,pathlib,subprocess
root=pathlib.Path('/home/zbs/rf-round7-live-window-20261019-attempt2')
print(json.dumps({'state':json.loads((root/'execution/state.json').read_text()),'proofs':{p.name:json.loads(p.read_text())['native_permanent_uuids'] for p in (root/'execution').glob('*-uuid-proof.json')},'commit':[s[:1700] for s in (root/'execution/service-fallback-scheduler.log').read_text().splitlines() if 'rf-job UUID allocation' in s or 'rf-job deterministic fallback' in s], 'du':subprocess.run(['du','-sh',str(root/'execution')],capture_output=True,text=True).stdout}))
