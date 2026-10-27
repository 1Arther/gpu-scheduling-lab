import datetime,json,pathlib,subprocess,hashlib
root=pathlib.Path('/home/zbs/rf-round7-live-window-20261019-attempt2');k=['sudo','-n','kubectl','--kubeconfig=/etc/kubernetes/admin.conf']
def get(kind,name=None,ns=None):
 p=subprocess.run(k+['get',kind]+([name] if name else [])+(['-n',ns] if ns else (['-A'] if kind=='pods' else []))+['-o','json','--request-timeout=15s'],capture_output=True,text=True,timeout=25);p.check_returncode();return json.loads(p.stdout)
orig=get('deployment','volcano-scheduler','volcano-system');cm=get('configmap','volcano-scheduler-configmap','volcano-system');rf=get('deployment','volcano-rf-test','gpu-rf-system');pods=get('pods')['items'];node=get('node','t3dgq');dqn=get('deployment','dqn-scheduler-grpc','volcano-system')
import sys;sys.path.insert(0,str(root/'lab/scripts'));from round7_window_preflight import gpu_demand
active=[dict(name=p['metadata']['name'],namespace=p['metadata']['namespace'],uid=p['metadata']['uid'],phase=p['status'].get('phase')) for p in pods if p['status'].get('phase') not in ['Succeeded','Failed'] and gpu_demand(p)]
cri=subprocess.run(['sudo','-n','crictl','ps','-o','json'],capture_output=True,text=True,check=True)
cid=next(c['id'] for c in json.loads(cri.stdout)['containers'] if c['metadata']['name']=='volcano-scheduler')
info=json.loads(subprocess.run(['sudo','-n','crictl','inspect','-o','json',cid],capture_output=True,text=True,check=True).stdout)
pid=info['info']['pid'];sha=subprocess.run(['sudo','-n','sha256sum','/proc/'+str(pid)+'/exe'],capture_output=True,text=True,check=True).stdout.split()[0]
result=dict(utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),original_scheduler_replicas=orig['spec']['replicas'],original_scheduler_available=orig['status'].get('availableReplicas',0),
 original_spec_equal=orig['spec']==json.loads((root/'deployment-original.json').read_text())['spec'],original_config_equal=cm['data']==json.loads((root/'configmap-original.json').read_text())['data'],original_binary_sha256=sha,config_resource_version=cm['metadata']['resourceVersion'],
 dqn_service_replicas=dqn['spec']['replicas'],dqn_service_available=dqn['status'].get('availableReplicas',0),rf_scheduler_replicas=rf['spec']['replicas'],rf_config=next(v['configMap']['name'] for v in rf['spec']['template']['spec']['volumes'] if v['name']=='config'),
 own_test_pods=[p['metadata']['name'] for p in pods if p['metadata']['namespace']=='gpu-rf-tests'],rf_scheduler_pods=[p['metadata']['name'] for p in pods if p['metadata']['namespace']=='gpu-rf-system'],active_gpu_dra_pods=active,
 target_node_lock=node['metadata'].get('annotations',{}).get('hamivgpu'),own_podgroups=len(get('podgroups',ns='gpu-rf-tests')['items']),
 retained_objects='Stopped RF Deployment, test namespaces, RBAC, Queue/Quota, ConfigMaps and expired independent Lease retained for audit/reproduction; no active Pod/GPU reservation')
assert result['original_scheduler_replicas']==result['original_scheduler_available']==1 and result['original_spec_equal'] and result['original_config_equal'] and sha=='9f2e1ae3359ce234fd048bc48aab082add99a686631808908b5ae518bdebf4f4'
assert not active and not result['own_test_pods'] and not result['rf_scheduler_pods'] and not result['target_node_lock'] and result['own_podgroups']==0 and result['rf_scheduler_replicas']==0
(root/'execution/final-readonly-verification.json').write_text(json.dumps(result,indent=2));print(json.dumps(result))
