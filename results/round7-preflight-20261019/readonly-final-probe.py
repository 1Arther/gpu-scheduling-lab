import subprocess,json,hashlib,datetime,urllib.request
from pathlib import Path
k=['sudo','-n','kubectl','--kubeconfig=/etc/kubernetes/admin.conf']
def read(ident,args,project):
 r=subprocess.run(args,capture_output=True,text=True,timeout=15)
 result={'id':ident,'utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'command':args,'exit_code':r.returncode,'stderr':r.stderr}
 if r.returncode==0:result['data']=project(r.stdout)
 print(json.dumps(result),flush=True)
 return result.get('data')
read('online_binary_sha256',['sha256sum','/home/zbs/volcano-v1.13.1-dqn/_output/bin/vc-scheduler'],lambda s:s.strip())
read('online_scheduler_config',k+['get','configmap','volcano-scheduler-configmap','-n','volcano-system','-o','json','--request-timeout=8s'],lambda s:{'resourceVersion':json.loads(s)['metadata']['resourceVersion'],'data':json.loads(s)['data']})
read('experiment_namespaces',k+['get','namespace','gpu-rf-system','gpu-rf-tests','-o','json','--request-timeout=8s'],lambda s:json.loads(s))
p=read('scheduler_pod_address',k+['get','pods','-n','volcano-system','-l','app=volcano-scheduler','-o','json','--request-timeout=8s'],lambda s:[{'name':p['metadata']['name'],'ip':p['status'].get('podIP')} for p in json.loads(s)['items']])
for pod in p or []:
 url='http://'+pod['ip']+':8080/metrics'
 try:
  opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
  text=opener.open(url,timeout=2).read(2000000).decode()
  lines=[l for l in text.splitlines() if l.startswith('volcano_vgpu') or 'vgpu_device_allocated_' in l or 'vgpu_device_shared_number' in l]
  print(json.dumps({'id':'online_vgpu_metrics','url':url,'data':lines,'utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'source':'direct scheduler HTTP gauges if present'}))
 except Exception as exc:print(json.dumps({'id':'online_vgpu_metrics','url':url,'error':type(exc).__name__+': '+str(exc),'direct_ledger_observation_available':False}))
