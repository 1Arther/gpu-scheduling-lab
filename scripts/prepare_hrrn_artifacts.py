"""Generate local-only Stage 4 manifests; never invokes kubectl or Docker."""
from pathlib import Path
import copy
import json
import yaml

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'k8s/hrrn-stage4'
OUT.mkdir(parents=True, exist_ok=True)
def dump(path, objects):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump_all(objects, sort_keys=False, allow_unicode=True), encoding='utf-8')

configs=[]
for mode, enabled, complete, policy in [
    ('A-native-rf',False,False,'rf-job'),('B-hrrn-rf',True,False,'rf-job'),
    ('C-hrrn-complete-rf',True,True,'rf-job'),('D-native-binpack',False,False,'binpack'),
    ('E-native-spread',False,False,'spread')]:
    # Same actions, preceding comparators, node scoring and resource constraints.
    conf={'actions':'enqueue, allocate, backfill','tiers':[
        {'plugins':[{'name':'priority'},{'name':'gang','enablePreemptable':False},{'name':'conformance'}]},
        {'plugins':[{'name':'overcommit'},{'name':'drf','enablePreemptable':False},
            {'name':'predicates','arguments':{'predicate.CacheEnable':False}},
            {'name':'proportion'},{'name':'nodeorder'},{'name':'binpack'},
            {'name':'deviceshare','arguments':{
                'deviceshare.VGPUEnable':True,'deviceshare.GPUSharingEnable':False,
                'deviceshare.GPUNumberEnable':False,'deviceshare.NodeLockEnable':True,
                'deviceshare.SchedulePolicy':'binpack','deviceshare.ScheduleWeight':1,
                'deviceshare.GPUSelectPolicy':policy,'deviceshare.RFEnable':policy=='rf-job',
                'deviceshare.RFPredictionEnabled':True,
                'deviceshare.RFEndpoint':'http://127.0.0.1:18081/v1/rank',
                'deviceshare.RFModelVersion':'rf-v4-known-states-ee35cfabe30f',
                'deviceshare.RFTimeoutMS':1000,
                'deviceshare.KnownGeometriesCMName':'hrrn-s4-device-config',
                'deviceshare.KnownGeometriesCMNamespace':'hrrn-lab-system'}},
            {'name':'hrrn','enableJobOrder':True,'enableJobReady':True,'arguments':{
                'enabled':enabled,'complete-job':complete,'tau-seconds':1.0,'default-duration-seconds':60.0}}]}]}
    text=yaml.safe_dump(conf,sort_keys=False)
    (OUT / 'configs').mkdir(exist_ok=True)
    (OUT / 'configs' / (mode+'.yaml')).write_text(text,encoding='utf-8')
    configs.append({'apiVersion':'v1','kind':'ConfigMap','metadata':{'name':'hrrn-s4-'+mode.lower(),'namespace':'hrrn-lab-system'},'data':{'volcano-scheduler.conf':text}})
old=ROOT/'k8s/round7-logical-window'
device=list(yaml.safe_load_all((old/'03-configmaps.yaml').read_text(encoding='utf-8')))[-1]
device['metadata']={'name':'hrrn-s4-device-config','namespace':'hrrn-lab-system'}
dump(OUT/'03-configmaps.yaml',configs+[device])
dump(OUT/'00-namespaces.yaml',[{'apiVersion':'v1','kind':'Namespace','metadata':{'name':name,'labels':{'gpu-lab/experiment':'hrrn-stage4'}}} for name in ['hrrn-lab-system','hrrn-lab-tests']])
text=(old/'01-rbac.yaml').read_text(encoding='utf-8')
for a,b in [('volcano-rf-test','volcano-hrrn-lab'),('rf-v7','hrrn-s4'),('gpu-rf-system','hrrn-lab-system'),('gpu-rf-tests','hrrn-lab-tests'),('round7-rf','hrrn-stage4'),('t3dgq','review-required-node')]:text=text.replace(a,b)
(OUT/'01-rbac-DRAFT.yaml').write_text(text,encoding='utf-8')
dump(OUT/'02-queue-quota.yaml',[
    {'apiVersion':'scheduling.volcano.sh/v1beta1','kind':'Queue','metadata':{'name':'hrrn-s4-queue'},'spec':{'weight':1,'reclaimable':False,'parent':'root','capability':{'cpu':'4','memory':'8Gi','volcano.sh/vgpu-number':'8','volcano.sh/vgpu-memory':'65536','volcano.sh/vgpu-cores':'800'}}},
    {'apiVersion':'v1','kind':'ResourceQuota','metadata':{'name':'hrrn-s4-budget','namespace':'hrrn-lab-tests'},'spec':{'hard':{'pods':'16','requests.cpu':'4','requests.memory':'8Gi','requests.volcano.sh/vgpu-number':'16','requests.volcano.sh/vgpu-memory':'131072','requests.volcano.sh/vgpu-cores':'1600'}}}])
dep=yaml.safe_load((old/'04-scheduler-DRAFT.yaml').read_text(encoding='utf-8'))
text=yaml.safe_dump(dep,sort_keys=False)
for a,b in [('volcano-rf-test','volcano-hrrn-lab'),('rf-v7','hrrn-s4'),('gpu-rf-system','hrrn-lab-system'),('round7-rf','hrrn-stage4'),('t3dgq','review-required-node'),('/vc-scheduler-rf','/vc-scheduler-hrrn'),('gpu-lab/volcano-rf:round7-build-required','gpu-lab/volcano-hrrn:stage4-build-required'),('gpu-lab/rf-model:round7-build-required','gpu-lab/rf-model:stage4-frozen-build-required'),('hrrn-s4-config-on','hrrn-s4-a-native-rf')]:text=text.replace(a,b)
dep=yaml.safe_load(text)
assert dep['spec']['replicas']==0
dump(OUT/'04-scheduler-DRAFT.yaml',[dep])

def job(name, seconds, estimate, cores=100, number=1, replicas=1, gated=True, minimum=None, kind='training'):
    resources={'cpu':'100m','memory':'32Mi','volcano.sh/vgpu-number':str(number),'volcano.sh/vgpu-memory':'4096','volcano.sh/vgpu-cores':str(cores)}
    ps={'schedulerName':'volcano-hrrn-lab','restartPolicy':'Never','activeDeadlineSeconds':300,
        'terminationGracePeriodSeconds':3,'nodeSelector':{'kubernetes.io/hostname':'review-required-node'},
        'containers':[{'name':'sleep','image':'busybox:1.36.1','command':['/bin/sh','-c',f'exec sleep {seconds}'],
            'resources':{'requests':resources,'limits':{**resources,'cpu':'200m','memory':'64Mi'}},
            'securityContext':{'allowPrivilegeEscalation':False,'capabilities':{'drop':['ALL']}}}]}
    if gated: ps['schedulingGates']=[{'name':'gpu-lab/ready'}]
    return {'apiVersion':'batch.volcano.sh/v1alpha1','kind':'Job',
        'metadata':{'name':name,'namespace':'hrrn-lab-tests','labels':{'gpu-lab/experiment':'hrrn-stage4'},
            'annotations':{'gpu-lab/estimated-duration-seconds':str(estimate),'gpu-lab/total-pods':str(replicas)}},
        'spec':{'schedulerName':'volcano-hrrn-lab','queue':'hrrn-s4-queue','minAvailable':replicas if minimum is None else minimum,'maxRetry':0,
            'tasks':[{'name':'worker','replicas':replicas,'minAvailable':replicas if minimum is None else minimum,
                'template':{'metadata':{'labels':{'gpu-lab/workload-type':kind,'gpu-lab/experiment':'hrrn-stage4'}},'spec':ps}}]}}
dump(OUT/'jobs/01-blocker.yaml',[job('blocker',45,45,gated=False)])
dump(OUT/'jobs/02-long.yaml',[job('long',20,100)])
dump(OUT/'jobs/03-short.yaml',[job('short',5,10,kind='inference')])
dump(OUT/'jobs/04-multi-gpu.yaml',[job('multi-gpu',10,10,number=2)])
dump(OUT/'jobs/05-two-pods.yaml',[job('two-pods',10,10,cores=60,replicas=2,minimum=1)])
dump(OUT/'jobs/06-small-bypass.yaml',[job('small-bypass',5,100)])
(OUT/'Dockerfile.scheduler').write_text('ARG BASE_IMAGE=docker.io/volcanosh/vc-scheduler:v1.13.1\nFROM ${BASE_IMAGE}\nCOPY --chmod=0555 results/hrrn-native-stage4-20261026/bin/vc-scheduler-hrrn /vc-scheduler-hrrn\nUSER 1000:1000\nENTRYPOINT ["/vc-scheduler-hrrn"]\n',encoding='utf-8')
# Same RF Dockerfile/model/runtime. Nothing is trained or modified.
(OUT/'Dockerfile.rf').write_bytes((ROOT/'k8s/round7/Dockerfile.rf').read_bytes())
checks={'deployment_replicas':dep['spec']['replicas'],'configs':len(configs),'jobs':6,'cluster_contacted':False,'image_built':False}
(ROOT/'results/hrrn-native-stage4-20261026/manifest-generation.json').write_text(json.dumps(checks,indent=2),encoding='utf-8')
print(json.dumps(checks))
