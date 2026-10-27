import datetime,json,pathlib,subprocess
root=pathlib.Path('/home/zbs/rf-round7-live-window-20261019-attempt2');state=json.loads((root/'execution/state.json').read_text());k=['sudo','-n','kubectl','--kubeconfig=/etc/kubernetes/admin.conf'];events=[]
def run(args,body=None):
 p=subprocess.run(k+args,input=json.dumps(body) if body is not None else None,capture_output=True,text=True,timeout=30);events.append(dict(command=k+args,exit_code=p.returncode,stdout=p.stdout,stderr=p.stderr))
 if p.returncode:raise RuntimeError(p.stderr)
 return json.loads(p.stdout) if p.stdout.strip().startswith('{') else p.stdout
rf=run(['get','deployment','volcano-rf-test','-n','gpu-rf-system','-o','json']);assert rf['spec']['replicas']==0
pods=run(['get','pods','-n','gpu-rf-tests','-o','json'])['items'];assert not pods
# Restore ONLY the stopped experimental deployment's normal RF configuration.
run(['patch','deployment','volcano-rf-test','-n','gpu-rf-system','--type=strategic','-p',json.dumps({'spec':{'template':{'spec':{'volumes':[{'name':'config','configMap':{'name':'rf-v7-config-on'}}]}}}})])
known={r['uid'] for r in state['created'] if r['kind']=='PodGroup'}
for pg in run(['get','podgroups','-n','gpu-rf-tests','-o','json'])['items']:
 m=pg['metadata']
 if m['uid'] not in known or m.get('labels',{}).get('gpu-lab/experiment')!='round7-rf':raise RuntimeError('Foreign PodGroup; refuse cleanup')
 run(['delete','--raw=/apis/scheduling.volcano.sh/v1beta1/namespaces/gpu-rf-tests/podgroups/'+m['name'],'-f','-'],dict(apiVersion='v1',kind='DeleteOptions',preconditions=dict(uid=m['uid'])))
record=dict(utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),own_pods_remaining=0,rf_replicas=0,own_rf_default_config_restored=True,original_scheduler_modified=False,operations=events)
(root/'execution/final-own-cleanup.json').write_text(json.dumps(record,indent=2));print(json.dumps(record))
