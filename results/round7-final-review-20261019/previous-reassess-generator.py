"""Generate review-only logical-allocation window and canary drafts. No kubectl."""
from copy import deepcopy
import difflib
import json
from pathlib import Path
import yaml

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'k8s/round7-logical-window'
RAW=ROOT/'results/round7-reassessment-20261019/raw/cluster.jsonl'


def write(name,value):
    (OUT/name).write_text(yaml.safe_dump_all(value if isinstance(value,list) else [value],sort_keys=False,allow_unicode=True),encoding='utf-8')


def main():
    OUT.mkdir(exist_ok=True)
    for name in ['00-namespaces.yaml','02-queue-quota.yaml','03-configmaps.yaml']:
        (OUT/name).write_bytes((ROOT/'k8s/round7'/name).read_bytes())
    rbac=list(yaml.safe_load_all((ROOT/'k8s/round7/01-rbac.yaml').read_text()))
    for obj in rbac:
        for rule in obj.get('rules',[]):
            if rule.get('resourceNames')==['rf-exclusive-node-required']:rule['resourceNames']=['t3dgq']
    write('01-rbac.yaml',rbac)
    dep=yaml.safe_load((ROOT/'k8s/round7/04-scheduler-DRAFT.yaml').read_text())
    args=dep['spec']['template']['spec']['containers'][0]['args']
    args[args.index('--node-selector=gpu-lab/partition:rf-exclusive-v7')]='--node-selector=kubernetes.io/hostname:t3dgq'
    write('04-scheduler-DRAFT.yaml',dep) # replicas remain ZERO, images remain placeholders
    pods=[]
    for p in sorted((ROOT/'k8s/round7').glob('test-*.yaml')):
        docs=list(yaml.safe_load_all(p.read_text()))
        for obj in docs:
            if obj['kind']=='Pod':
                obj['spec']['nodeSelector']={'kubernetes.io/hostname':'t3dgq'}
                obj['spec']['tolerations']=[{'key':k,'operator':'Exists','effect':'NoSchedule'} for k in ['node-role.kubernetes.io/control-plane','node-role.kubernetes.io/master']]
                for c in obj['spec']['containers']:
                    for typ in ['requests','limits']:
                        for key in ['volcano.sh/vgpu-number','volcano.sh/vgpu-memory','volcano.sh/vgpu-cores']:
                            c['resources'][typ][key]=str(c['resources'][typ][key])
                pods.append(deepcopy(obj))
        write(p.name,docs)
    sample=deepcopy(pods[0]);image=sample['spec']['containers'][0]['image']
    # Explicit guards make missing optional Pod fields safe. A vGPU "memory"
    # Quantity here is the plugin's integer MiB contract, not byte capacity.
    gpu_key="k == 'nvidia.com/gpu' || k.startsWith('volcano.sh/vgpu-')"
    variables=[
      {'name':'containers','expression':"object.spec.containers + (has(object.spec.initContainers) ? object.spec.initContainers : [])"},
      {'name':'dra','expression':"has(object.spec.resourceClaims) && object.spec.resourceClaims.size() > 0"},
      {'name':'preallocated','expression':"has(object.metadata.annotations) && object.metadata.annotations.exists(k, k in ['volcano.sh/vgpu-ids-new','volcano.sh/devices-to-allocate','volcano.sh/vgpu-node'])"},
      {'name':'gpu','expression':f"variables.dra || variables.preallocated || variables.containers.exists(c, has(c.resources) && ((has(c.resources.limits) && c.resources.limits.exists(k, {gpu_key})) || (has(c.resources.requests) && c.resources.requests.exists(k, {gpu_key}))))"},
    ]
    shape=[
      "object.metadata.namespace == 'gpu-rf-tests'",
      "has(object.metadata.labels) && 'gpu-lab/experiment' in object.metadata.labels && object.metadata.labels['gpu-lab/experiment'] == 'round7-rf'",
      "'gpu-lab/workload-type' in object.metadata.labels && object.metadata.labels['gpu-lab/workload-type'] in ['training','inference']",
      "object.metadata.name.startsWith('rf-v7-')",
      "object.spec.schedulerName == 'volcano-rf-test'",
      "!has(object.spec.nodeName) || object.spec.nodeName == ''",
      "has(object.spec.nodeSelector) && 'kubernetes.io/hostname' in object.spec.nodeSelector && object.spec.nodeSelector['kubernetes.io/hostname'] == 't3dgq'",
      "has(object.spec.runtimeClassName) && object.spec.runtimeClassName == 'nvidia'",
      "!variables.dra",
      "!variables.preallocated",
      "!has(object.spec.initContainers) || object.spec.initContainers.size() == 0",
      "object.spec.containers.size() == 1",
      "!has(object.spec.hostPID) || !object.spec.hostPID",
      "!has(object.spec.hostNetwork) || !object.spec.hostNetwork",
      "!has(object.spec.volumes) || object.spec.volumes.size() == 0",
      "has(object.spec.automountServiceAccountToken) && !object.spec.automountServiceAccountToken",
    ]
    container=[f"c.image == '{image}'","has(c.command) && c.command == ['/bin/sleep','600']",
               "!has(c.args) || c.args.size() == 0","!has(c.env) || c.env.size() == 0",
               "!has(c.envFrom) || c.envFrom.size() == 0",
               "!has(c.lifecycle)","!has(c.livenessProbe)","!has(c.startupProbe)","!has(c.readinessProbe)",
               "has(c.resources) && has(c.resources.requests) && has(c.resources.limits)"]
    for typ in ['requests','limits']:
        m='c.resources.'+typ
        container.extend([f"'{key}' in {m} && {m}['{key}'] == '{val}'" for key,val in [('volcano.sh/vgpu-number','1'),('volcano.sh/vgpu-memory','4096'),('volcano.sh/vgpu-cores','20')]])
        container.append(f"{m}.all(k, !(k == 'nvidia.com/gpu' || k.startsWith('volcano.sh/vgpu-')) || k in ['volcano.sh/vgpu-number','volcano.sh/vgpu-memory','volcano.sh/vgpu-cores'])")
    shape.append('object.spec.containers.all(c, '+' && '.join('('+s+')' for s in container)+')')
    expression='!variables.gpu || ('+' && '.join('('+s+')' for s in shape)+')'
    meta={'name':'rf-v7-gpu-window','labels':{'gpu-lab/experiment':'round7-rf'}}
    policy={'apiVersion':'admissionregistration.k8s.io/v1','kind':'ValidatingAdmissionPolicy','metadata':meta,
      'spec':{'failurePolicy':'Fail','matchConstraints':{'resourceRules':[{'apiGroups':[''],'apiVersions':['v1'],'operations':['CREATE'],'resources':['pods']}]},
              'variables':variables,'validations':[{'expression':expression,'message':'RF logical-allocation window: new GPU/DRA Pods are paused except approved sleep cases. Contact experiment owner; no existing GPU Pod is deleted.'}]}}
    binding={'apiVersion':'admissionregistration.k8s.io/v1','kind':'ValidatingAdmissionPolicyBinding','metadata':deepcopy(meta),
      'spec':{'policyName':meta['name'],'validationActions':['Deny']}}
    write('05-window-policy-DRAFT.yaml',policy);write('06-window-binding-DRAFT.yaml',binding)
    cases=[{'name':'approved-training','pod':sample,'allowed':True}]
    inference=deepcopy(sample);inference['metadata']['labels']['gpu-lab/workload-type']='inference'
    cases.append({'name':'approved-inference','pod':inference,'allowed':True})
    nongpu={'metadata':{'namespace':'default','name':'ordinary-cpu'},'spec':{'containers':[{'name':'cpu','image':'existing-cpu-image'}]}}
    cases.append({'name':'non-gpu-business','pod':nongpu,'allowed':True})
    def deny(name,edit):
        p=deepcopy(sample);edit(p);cases.append({'name':name,'pod':p,'allowed':False})
    deny('old-dqn',lambda p:p['spec'].update(schedulerName='volcano'))
    deny('default-scheduler',lambda p:p['spec'].update(schedulerName='default-scheduler'))
    deny('missing-experiment-label',lambda p:p['metadata']['labels'].pop('gpu-lab/experiment'))
    deny('wrong-workload',lambda p:p['metadata']['labels'].update({'gpu-lab/workload-type':'other'}))
    deny('foreign-namespace',lambda p:p['metadata'].update(namespace='default'))
    deny('direct-nodeName',lambda p:p['spec'].update(nodeName='t3dgq'))
    deny('other-node',lambda p:p['spec']['nodeSelector'].update({'kubernetes.io/hostname':'t3dmg'}))
    deny('cuda-command',lambda p:p['spec']['containers'][0].update(command=['python','train.py']))
    deny('different-image',lambda p:p['spec']['containers'][0].update(image='other'))
    deny('quota-changed',lambda p:p['spec']['containers'][0]['resources']['limits'].update({'volcano.sh/vgpu-memory':'8192'}))
    deny('ordinary-gpu-alias',lambda p:p['spec']['containers'][0]['resources']['limits'].update({'nvidia.com/gpu':'1'}))
    deny('additional-container',lambda p:p['spec']['containers'].append(deepcopy(p['spec']['containers'][0])|{'name':'extra'}))
    deny('init-gpu',lambda p:p['spec'].update(initContainers=[deepcopy(p['spec']['containers'][0])|{'name':'init'}]))
    deny('DRA',lambda p:p['spec'].update(resourceClaims=[{'name':'test','resourceClaimName':'existing-claim'}]))
    for typ in ['limits','requests']:
        p=deepcopy(nongpu);p['spec']['containers'][0]['resources']={typ:{'nvidia.com/gpu':'1'}}
        cases.append({'name':'ordinary-'+typ+'-only','pod':p,'allowed':False})
    p=deepcopy(nongpu);p['metadata']['annotations']={'volcano.sh/vgpu-ids-new':'GPU-forged,NVIDIA,4096,20:'}
    cases.append({'name':'preallocated-uuid-without-request','pod':p,'allowed':False})
    (OUT/'policy-cases.json').write_text(json.dumps({'source':'synthetic protocol fixtures, NOT real admission outcomes','variables':variables,'expression':expression,'cases':cases},indent=2),encoding='utf-8')
    # Optional three-model scope. Still replicas=0; other nodes require cache
    # checks and owner consent. Cache accepts repeated key:value pairs with OR.
    expand=OUT/'expand-three-nodes';expand.mkdir(exist_ok=True)
    def expanded_write(name,objects):
        (expand/name).write_text(yaml.safe_dump_all(objects if isinstance(objects,list) else [objects],sort_keys=False),encoding='utf-8')
    names=['t3dgq','t3dmg','t3dgq-2']
    dep3=deepcopy(dep);args3=dep3['spec']['template']['spec']['containers'][0]['args']
    args3[:]=[s for s in args3 if not s.startswith('--node-selector=')]+['--node-selector=kubernetes.io/hostname:'+n for n in names]
    expanded_write('04-scheduler-DRAFT.yaml',dep3)
    role3=deepcopy(next(r for r in rbac if r['kind']=='ClusterRole'))
    for rule in role3['rules']:
        if rule.get('resourceNames')==['t3dgq']:rule['resourceNames']=names
    expanded_write('01-node-rbac-DRAFT.yaml',role3)
    old_clause="has(object.spec.nodeSelector) && 'kubernetes.io/hostname' in object.spec.nodeSelector && object.spec.nodeSelector['kubernetes.io/hostname'] == 't3dgq'"
    new_clause="!has(object.spec.nodeSelector) || !('kubernetes.io/hostname' in object.spec.nodeSelector) || object.spec.nodeSelector['kubernetes.io/hostname'] in ['t3dgq','t3dmg','t3dgq-2']"
    expr3=expression.replace(old_clause,new_clause);assert expr3!=expression
    policy3=deepcopy(policy);policy3['spec']['validations'][0]['expression']=expr3
    expanded_write('05-window-policy-DRAFT.yaml',policy3)
    cases3=deepcopy(cases)
    next(c for c in cases3 if c['name']=='other-node')['allowed']=True
    freepod=deepcopy(sample);freepod['spec'].pop('nodeSelector')
    freepod['spec']['affinity']={'nodeAffinity':{'requiredDuringSchedulingIgnoredDuringExecution':{'nodeSelectorTerms':[{'matchExpressions':[{'key':'kubernetes.io/hostname','operator':'In','values':names}]}]}}}
    cases3.append({'name':'three-node-native-affinity','pod':freepod,'allowed':True})
    a40=deepcopy(sample);a40['spec']['nodeSelector']['kubernetes.io/hostname']='r5300'
    cases3.append({'name':'A40-excluded','pod':a40,'allowed':False})
    (expand/'policy-cases.json').write_text(json.dumps({'source':'synthetic protocol fixtures, NOT real admission outcomes','variables':variables,'expression':expr3,'cases':cases3},indent=2),encoding='utf-8')
    for p in sorted(OUT.glob('test-g-*.yaml')):
        docs=list(yaml.safe_load_all(p.read_text()))
        for obj in docs:
            if obj['kind']=='Pod':
                obj['spec'].pop('nodeSelector');obj['spec']['affinity']=deepcopy(freepod['spec']['affinity'])
        expanded_write(p.name,docs)
    deny_probe=next(c['pod'] for c in cases if c['name']=='old-dqn')
    write('probe-denied-DRAFT.yaml',{'apiVersion':'v1','kind':'Pod',**deny_probe})
    # Scheme A is a review patch, NEVER applied to the existing scheduler here.
    records=[json.loads(s) for s in RAW.read_text(encoding='utf-8').splitlines()]
    cm=next(r['data'] for r in records if r['id']=='configmap:volcano-system/volcano-scheduler-configmap')
    before=cm['data']['volcano-scheduler.conf'];cfg=yaml.safe_load(before)
    plugin=next(p for t in cfg['tiers'] for p in t['plugins'] if p['name']=='deviceshare')
    plugin['arguments'].update({'deviceshare.RFEnable':True,'deviceshare.RFCanaryEnable':True,
      'deviceshare.RFCanaryNamespace':'gpu-rf-tests','deviceshare.RFCanaryScheduleWeight':1,
      'deviceshare.RFEndpoint':'http://127.0.0.1:18081/v1/rank','deviceshare.RFTimeoutMS':500,
      'deviceshare.RFModelVersion':'rf-v4-known-states-ee35cfabe30f','deviceshare.RFPredictionEnabled':True})
    after=yaml.safe_dump(cfg,sort_keys=False)
    write('A-canary-config-DRAFT.yaml',{'apiVersion':'v1','kind':'ConfigMap','metadata':{'name':'rf-v7-canary-config','namespace':'volcano-system','labels':{'gpu-lab/experiment':'round7-rf'}},'data':{'volcano-scheduler.conf':after}})
    (OUT/'A-config.diff').write_text(''.join(difflib.unified_diff(before.splitlines(True),after.splitlines(True),fromfile='online-config-unchanged',tofile='new-canary-config-DRAFT')),encoding='utf-8')
    write('A-scheduler-patch-DRAFT.yaml',{'spec':{'strategy':{'type':'Recreate','rollingUpdate':None},'template':{'spec':{
      'automountServiceAccountToken':False,
      'containers':[{'name':'volcano-scheduler','image':'gpu-lab/volcano-rf:canary-build-required','command':['/vc-scheduler-rf'],
        'volumeMounts':[{'mountPath':'/vc-scheduler','$patch':'delete'},
          {'name':'rf-api-token','mountPath':'/var/run/secrets/kubernetes.io/serviceaccount','readOnly':True}]},
        deepcopy(dep['spec']['template']['spec']['containers'][1])],
      'volumes':[{'name':'local-vc-scheduler','$patch':'delete'},{'name':'volcano-scheduler-config','configMap':{'name':'rf-v7-canary-config'}},
         {'name':'rf-tmp','emptyDir':{}},{'name':'rf-api-token','projected':deepcopy(dep['spec']['template']['spec']['volumes'][-1]['projected'])}]}}}})
    gate={'deployment_allowed':False,'recommended_conditional_scheme':'B_time_bounded_admission_window',
      'physical_occupancy_alone_is_not_a_blocker':True,'no_cuda_or_physical_memory_preallocation':True,
      'exclusive_logical_admission_window_installed':False,'admission_propagation_verified':False,
      'no_prior_pending_or_unbound_gpu_reservations_confirmed_after_guard':False,
      'owners_approve_sleep_startup_overhead':False,'new_gpu_submission_pause_approved':False,
      'phase2_explicit_user_approval':False,'model_unchanged':True,
      'initial_node':'t3dgq','initial_uuid_count':4,'other_nodes':'t3dmg/t3dgq-2 require image-cache and explicit startup consent checks',
      'scheduler_image_digest':None,'rf_image_digest':None,'note':'No guard, scheduler, namespace or Pod created. Do not apply drafts.'}
    (OUT/'execution-gate.json').write_text(json.dumps(gate,indent=2),encoding='utf-8')
    print(json.dumps({'drafts':len(list(OUT.glob('*.yaml'))),'policy_fixture_cases':len(cases),'cluster_resources_changed':False}))


if __name__=='__main__':main()
