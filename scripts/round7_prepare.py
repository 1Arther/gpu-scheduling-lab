"""Generate review-only manifests from frozen artifacts. NEVER calls kubectl."""
import argparse
import json
from pathlib import Path
import yaml

ROOT=Path(__file__).resolve().parents[1]
SYSTEM='gpu-rf-system';TEST='gpu-rf-tests';QUEUE='rf-v7-queue'
LABEL={'gpu-lab/experiment':'round7-rf'}
PINNED_SLEEP='docker.io/pytorch/pytorch@sha256:923f687790bec78081c357e71dcd5dcef80b0cc00f6c34484902a5e83362c854'


def obj(kind,name,namespace=None,api='v1',**fields):
    m={'name':name,'labels':LABEL.copy()}
    if namespace:m['namespace']=namespace
    return dict(apiVersion=api,kind=kind,metadata=m,**fields)


def config(name,version,enabled=True,prediction=True,policy='rf-job',endpoint='http://127.0.0.1:18081/v1/rank'):
    args={'deviceshare.VGPUEnable':True,'deviceshare.GPUSharingEnable':False,'deviceshare.GPUNumberEnable':False,
          'deviceshare.NodeLockEnable':True,'deviceshare.SchedulePolicy':'binpack','deviceshare.ScheduleWeight':1,
          'deviceshare.GPUSelectPolicy':policy,'deviceshare.RFEnable':enabled,'deviceshare.RFPredictionEnabled':prediction,
          'deviceshare.RFEndpoint':endpoint,'deviceshare.RFModelVersion':version,'deviceshare.RFTimeoutMS':500}
    args.update({'deviceshare.KnownGeometriesCMName':'rf-v7-device-config','deviceshare.KnownGeometriesCMNamespace':SYSTEM})
    conf={'actions':'enqueue, allocate','tiers':[{'plugins':[{'name':'priority'},{'name':'gang','enablePreemptable':False},{'name':'conformance'}]},
          {'plugins':[{'name':'deviceshare','arguments':args},{'name':'predicates'}]}]}
    return obj('ConfigMap',name,SYSTEM,data={'volcano-scheduler.conf':yaml.safe_dump(conf,sort_keys=False)})


def generate(out):
    out.mkdir(parents=True,exist_ok=True)
    version=json.loads((ROOT/'models/pod-gpu-rf-v1/manifest.json').read_text())['model_version']
    def save(name,documents):
        (out/name).write_text(yaml.safe_dump_all(documents,sort_keys=False,allow_unicode=True),encoding='utf-8')
    save('00-namespaces.yaml',[obj('Namespace',SYSTEM),obj('Namespace',TEST)])
    read_rules=[{'apiGroups':[''],'resources':['nodes','pods','namespaces','services','replicationcontrollers','persistentvolumeclaims','persistentvolumes','resourcequotas'],'verbs':['get','list','watch']},
                {'apiGroups':['apps'],'resources':['replicasets','statefulsets','daemonsets'],'verbs':['get','list','watch']},
                {'apiGroups':['storage.k8s.io'],'resources':['storageclasses','csinodes','csidrivers','csistoragecapacities','volumeattachments'],'verbs':['get','list','watch']},
                {'apiGroups':['scheduling.k8s.io'],'resources':['priorityclasses'],'verbs':['get','list','watch']},
                {'apiGroups':['policy'],'resources':['poddisruptionbudgets'],'verbs':['get','list','watch']},
                {'apiGroups':['scheduling.volcano.sh'],'resources':['queues','podgroups'],'verbs':['get','list','watch']},
                {'apiGroups':['nodeinfo.volcano.sh'],'resources':['numatopologies'],'verbs':['get','list','watch']},
                {'apiGroups':['topology.volcano.sh'],'resources':['hypernodes'],'verbs':['get','list','watch']},
                {'apiGroups':['resource.k8s.io'],'resources':['resourceclaims','resourceclaims/status','deviceclasses','resourceslices','devicetaintrules'],'verbs':['get','list','watch']},
                {'apiGroups':['scheduling.volcano.sh'],'resources':['queues'],'verbs':['create']},
                {'apiGroups':[''],'resources':['nodes'],'resourceNames':['rf-exclusive-node-required'],'verbs':['update','patch']}]
    sa=obj('ServiceAccount','volcano-rf-test',SYSTEM)
    role=obj('ClusterRole','rf-v7-scheduler',api='rbac.authorization.k8s.io/v1',rules=read_rules)
    subject=[{'kind':'ServiceAccount','name':'volcano-rf-test','namespace':SYSTEM}]
    binding=obj('ClusterRoleBinding','rf-v7-scheduler',api='rbac.authorization.k8s.io/v1',roleRef={'apiGroup':'rbac.authorization.k8s.io','kind':'ClusterRole','name':role['metadata']['name']},subjects=subject)
    docs=[sa,role,binding]
    for ns in (SYSTEM,TEST):
        rules=[{'apiGroups':[''],'resources':['events'],'verbs':['get','list','watch','create','update','patch']},
               {'apiGroups':[''],'resources':['pods'],'verbs':['patch']},
               {'apiGroups':[''],'resources':['pods/status'],'verbs':['update','patch']},
               {'apiGroups':[''],'resources':['pods/binding'],'verbs':['create']},
               {'apiGroups':['scheduling.volcano.sh'],'resources':['podgroups','podgroups/status'],'verbs':['update','patch']}]
        if ns==SYSTEM:
            rules += [{'apiGroups':['coordination.k8s.io'],'resources':['leases'],'verbs':['get','list','watch','create','update','patch']}]
            rules += [{'apiGroups':[''],'resources':['configmaps'],'resourceNames':['rf-v7-device-config'],'verbs':['get']}]
        r=obj('Role','rf-v7-local-writes',ns,api='rbac.authorization.k8s.io/v1',rules=rules)
        b=obj('RoleBinding','rf-v7-local-writes',ns,api='rbac.authorization.k8s.io/v1',roleRef={'apiGroup':'rbac.authorization.k8s.io','kind':'Role','name':r['metadata']['name']},subjects=subject)
        docs.extend([r,b])
    save('01-rbac.yaml',docs)
    save('02-queue-quota.yaml',[obj('Queue',QUEUE,api='scheduling.volcano.sh/v1beta1',spec={'weight':1,'reclaimable':False,'parent':'root'}),
          obj('ResourceQuota','rf-v7-test-budget',TEST,spec={'hard':{'pods':'4','requests.volcano.sh/vgpu-number':'4',
              'requests.volcano.sh/vgpu-memory':'16384','requests.volcano.sh/vgpu-cores':'80'}})])
    variants=[config('rf-v7-config-on',version),config('rf-v7-config-no-prediction',version,prediction=False),
              config('rf-v7-config-native-binpack',version,enabled=False,policy='binpack'),
              config('rf-v7-config-service-unavailable',version,endpoint='http://127.0.0.1:1/v1/rank')]
    variants.append(obj('ConfigMap','rf-v7-device-config',SYSTEM,data={'device-config.yaml':yaml.safe_dump({'nvidia':{
        'resourceCountName':'volcano.sh/vgpu-number','resourceMemoryName':'volcano.sh/vgpu-memory','resourceCoreName':'volcano.sh/vgpu-cores',
        'resourceMemoryPercentageName':'volcano.sh/vgpu-memory-percentage','defaultGPUNum':1,'defaultMemory':0,'defaultCores':0,
        'deviceSplitCount':10,'deviceMemoryScaling':1,'deviceCoreScaling':1,'disableCoreLimit':False}},sort_keys=False)}))
    save('03-configmaps.yaml',variants)
    security={'allowPrivilegeEscalation':False,'readOnlyRootFilesystem':True,'capabilities':{'drop':['ALL']}}
    probe=['python','-c',f"import json,urllib.request; r=json.load(urllib.request.urlopen('http://127.0.0.1:18081/health',timeout=1)); assert r['model_ready'] and r['model_version']=={version!r}"]
    scheduler={'name':'scheduler','image':'gpu-lab/volcano-rf:round7-build-required','imagePullPolicy':'IfNotPresent',
          'command':['/vc-scheduler-rf'],'args':['--scheduler-name=volcano-rf-test','--scheduler-conf=/rf-config/volcano-scheduler.conf',
          '--default-queue='+QUEUE,'--node-selector=gpu-lab/partition:rf-exclusive-v7','--leader-elect=true',
          '--leader-elect-resource-name=volcano-rf-test','--leader-elect-resource-namespace='+SYSTEM,
          '--leader-elect-resource-lock=leases','--enable-healthz=true','--healthz-address=:18091',
          '--enable-metrics=true','--listen-address=:18090','--cache-dumper=false','--logtostderr','--v=4'],
          'securityContext':security,'resources':{'requests':{'cpu':'100m','memory':'256Mi'},'limits':{'cpu':'2','memory':'1Gi'}},
          'ports':[{'name':'metrics','containerPort':18090},{'name':'health','containerPort':18091}],
          'readinessProbe':{'httpGet':{'path':'/healthz','port':'health'},'periodSeconds':3},
          'volumeMounts':[{'name':'config','mountPath':'/rf-config','readOnly':True},{'name':'tmp','mountPath':'/tmp'},
             {'name':'api-token','mountPath':'/var/run/secrets/kubernetes.io/serviceaccount','readOnly':True}]}
    rf={'name':'rf-model','image':'gpu-lab/rf-model:round7-build-required','imagePullPolicy':'IfNotPresent',
        'command':['python','-m','gpu_lab.runtime.audit_server','--port','18081'],
        'env':[{'name':k,'value':'1'} for k in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS')],
        'securityContext':security,'resources':{'requests':{'cpu':'100m','memory':'256Mi'},'limits':{'cpu':'1','memory':'1Gi'}},
        'readinessProbe':{'exec':{'command':probe},'periodSeconds':3},
        'startupProbe':{'exec':{'command':probe},'periodSeconds':2,'failureThreshold':30},
        'volumeMounts':[{'name':'rf-tmp','mountPath':'/tmp'}]}
    ps={'serviceAccountName':'volcano-rf-test','automountServiceAccountToken':False,
        'securityContext':{'runAsNonRoot':True,'runAsUser':1000,'runAsGroup':1000,'fsGroup':1000,'seccompProfile':{'type':'RuntimeDefault'}},
        'nodeSelector':{'kubernetes.io/hostname':'t3dgq'},'containers':[scheduler,rf],
        'volumes':[{'name':'config','configMap':{'name':'rf-v7-config-on'}},{'name':'tmp','emptyDir':{}},{'name':'rf-tmp','emptyDir':{}},
                   {'name':'api-token','projected':{'sources':[{'serviceAccountToken':{'path':'token','expirationSeconds':3600}},
                    {'configMap':{'name':'kube-root-ca.crt','items':[{'key':'ca.crt','path':'ca.crt'}]}},
                    {'downwardAPI':{'items':[{'path':'namespace','fieldRef':{'fieldPath':'metadata.namespace'}}]}}]}}]}
    deploy=obj('Deployment','volcano-rf-test',SYSTEM,api='apps/v1',spec={'replicas':0,'strategy':{'type':'Recreate'},'revisionHistoryLimit':2,
          'selector':{'matchLabels':{'app':'volcano-rf-test'}},'template':{'metadata':{'labels':{**LABEL,'app':'volcano-rf-test'}},'spec':ps}})
    save('04-scheduler-DRAFT.yaml',[deploy])
    for case,task in [('a-training','training'),('b-inference','inference'),('c-first','training'),('c-second','inference'),
                      ('d-after-release','training'),('e-control','inference'),('f-fallback','inference'),('g-score-training','training'),('g-score-inference','inference')]:
        name='rf-v7-'+case
        pg=obj('PodGroup',name,TEST,api='scheduling.volcano.sh/v1beta1',spec={'minMember':1,'queue':QUEUE})
        resources={'volcano.sh/vgpu-number':1,'volcano.sh/vgpu-memory':4096,'volcano.sh/vgpu-cores':20}
        pod=obj('Pod',name,TEST,spec={'schedulerName':'volcano-rf-test','runtimeClassName':'nvidia','restartPolicy':'Never',
              'automountServiceAccountToken':False,'activeDeadlineSeconds':900,'terminationGracePeriodSeconds':5,
              'nodeSelector':{'gpu-lab/partition':'rf-exclusive-v7','kubernetes.io/hostname':'rf-exclusive-node-required'},
              'tolerations':[{'key':'gpu-lab/reserved','operator':'Equal','value':'rf-exclusive-v7','effect':'NoSchedule'}],
              'containers':[{'name':'sleep','image':PINNED_SLEEP,'imagePullPolicy':'Never','command':['/bin/sleep','600'],
                 'resources':{'requests':{**resources,'cpu':'100m','memory':'128Mi'},'limits':{**resources,'cpu':'500m','memory':'256Mi'}},
                 'securityContext':{'allowPrivilegeEscalation':False,'capabilities':{'drop':['ALL']}}}]})
        pod['metadata']['labels'].update({'gpu-lab/workload-type':task,'gpu-lab/case':case})
        pod['metadata']['annotations']={'scheduling.k8s.io/group-name':name}
        save('test-'+case+'.yaml',[pg,pod])
    contract={'deployment_allowed':False,'stage':'read_only_preparation','exclusive_node':None,'exclusive_gpu_uuids':[],
              'old_schedulers_cannot_select_target_node':False,'ordinary_gpu_and_vgpu_alias_protected':False,
              'host_gpu_users_have_released_all_target_devices':False,'admission_enforced_and_validated':False,
              'scheduler_image_digest':None,'rf_image_digest':None,'sleep_image':PINNED_SLEEP,
              'sleep_command_file_verified_on_4090':False,'explicit_phase2_user_approval':False,
              'reason':'NO-GO: no exclusive GPU proven; manifests are review drafts, replicas=0, target node is a placeholder'}
    (out/'isolation-gate.json').write_text(json.dumps(contract,indent=2),encoding='utf-8')
    print(json.dumps({'out':str(out),'model_version':version,'deployment_allowed':False,'yaml_files':len(list(out.glob('*.yaml')))}))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,default=ROOT/'k8s/round7');generate(p.parse_args().output)
