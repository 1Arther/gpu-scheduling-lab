from pathlib import Path
import json,hashlib,datetime
root=Path('/home/zbs/rf-round7-reassess-20261019')
out=root/'results/reassess-release-cpu'
rows=[]
for line in (out/'go-tests.log').read_text().splitlines():
 try:rows.append(json.loads(line))
 except ValueError:pass
trace=[r for r in rows if r.get('Test','').startswith('TestRFCanary') and any(s in r.get('Output','') for s in ['UUID allocation','logical release','DQNJobPolicy'])]
(out/'canary-native-trace.json').write_text(json.dumps({'source':'actual Go execution, fake Kubernetes client; DQN endpoint is a test stub','records':trace},indent=2))
advice=[]
for line in (out/'rf-advice.jsonl').read_text().splitlines():
 r=json.loads(line)
 if r.get('pod',{}).get('namespace')=='gpu-rf-tests':advice.append(r)
(out/'canary-live-rf-advice.json').write_text(json.dumps({'source':'actual frozen RF HTTP calls; SIM UUIDs and fake API; no real Pod','records':advice},indent=2))
meta={'go_pass_events_including_subtests':sum(r.get('Action')=='pass' and bool(r.get('Test')) for r in rows),
 'go_failure_events':sum(r.get('Action')=='fail' for r in rows),'skipped_tests':[r.get('Test') for r in rows if r.get('Action')=='skip'],
 'package_results':[{k:r.get(k) for k in ['Package','Action','Elapsed']} for r in rows if r.get('Action') in ['pass','fail'] and not r.get('Test')],
 'canary_live_model_advice_count':len(advice),'native_model_source':'frozen RF, NOT trained during test',
 'generated_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'cluster_resources_changed':False}
(out/'test-summary.json').write_text(json.dumps(meta,indent=2));print(json.dumps(meta,indent=2))
