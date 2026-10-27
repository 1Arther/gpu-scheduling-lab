import datetime,json,pathlib,subprocess
root=pathlib.Path('/home/zbs/rf-round7-live-window-20261019');out={'utc':datetime.datetime.now(datetime.timezone.utc).isoformat()}
for name in ['execution/state.json','execution/restore-confirmation.json']:
 p=root/name
 if p.exists():out[name]=json.loads(p.read_text())
p=root/'execution/events.jsonl'
if p.exists():out['events_tail']=[json.loads(x) for x in p.read_text().splitlines()[-8:]]
p=root/'execution-console.log'
if p.exists():out['console_tail']=p.read_text()[-6000:]
k=['sudo','-n','kubectl','--kubeconfig=/etc/kubernetes/admin.conf']
for ident,args in [('deployments',['get','deployments','-A','-o','json']),('test_pods',['get','pods','-n','gpu-rf-tests','-o','json','--ignore-not-found']),('rf_pods',['get','pods','-n','gpu-rf-system','-o','json','--ignore-not-found'])]:
 p=subprocess.run(k+args+['--request-timeout=10s'],capture_output=True,text=True,timeout=20)
 if p.returncode:out[ident]={'error':p.stderr};continue
 data=json.loads(p.stdout).get('items',[]) if p.stdout.strip() else []
 if ident=='deployments':out[ident]=[{'namespace':d['metadata']['namespace'],'name':d['metadata']['name'],'replicas':d['spec'].get('replicas'),'available':d.get('status',{}).get('availableReplicas',0)} for d in data if d['metadata']['name'] in ['volcano-scheduler','volcano-rf-test','dqn-scheduler-grpc']]
 else:out[ident]=[{'name':p['metadata']['name'],'uid':p['metadata']['uid'],'phase':p['status'].get('phase'),'node':p['spec'].get('nodeName'),'conditions':p['status'].get('conditions'),'containers':p['status'].get('containerStatuses'),'annotations':{k:v for k,v in p['metadata'].get('annotations',{}).items() if k.startswith('volcano.sh/')}} for p in data]
print(json.dumps(out,ensure_ascii=False))
