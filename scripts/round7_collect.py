"""Read-only stage-1 collector; run on management/GPU hosts via strict-key SSH.

No apply/delete/patch, exec, rollout, dry-run=server, auth-can-i or model service
startup. Kubernetes output excludes workload env/secret content. Timestamp every
command; logical reservations are reconstructed separately, never NVML-derived.
"""
import argparse
import base64
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import time


def emit(value):
    print(json.dumps(value,ensure_ascii=False),flush=True)


def run(ident,args,projection=None,sudo=False,timeout=20):
    start=dt.datetime.now(dt.timezone.utc).isoformat();clock=time.monotonic()
    try:
        r=subprocess.run(args,capture_output=True,timeout=timeout)
        if sudo and r.returncode:
            args=['sudo','-n']+args
            r=subprocess.run(args,capture_output=True,timeout=timeout)
        text=r.stdout.decode(errors='replace')
        row={'id':ident,'command':args,'started_utc':start,'elapsed_s':round(time.monotonic()-clock,3),
             'exit_code':r.returncode,'stderr':r.stderr.decode(errors='replace')[:2500],
             'raw_stdout_sha256':hashlib.sha256(r.stdout).hexdigest()}
        if not r.returncode and projection:
            row['data']=projection(json.loads(text))
        else:row['stdout']=text[:100000]
        emit(row);return row.get('data',text if not r.returncode else None)
    except Exception as exc:
        emit({'id':ident,'command':args,'started_utc':start,'exit_code':None,'error':type(exc).__name__+': '+str(exc)})


def meta(obj):
    m=obj.get('metadata',{})
    return {k:m.get(k) for k in ('name','namespace','uid','resourceVersion','creationTimestamp','deletionTimestamp')} | {
        'labels':m.get('labels',{}),'annotations':{k:v for k,v in m.get('annotations',{}).items()
        if k=='hamivgpu' or k.startswith(('volcano.sh/','scheduling.k8s.io/','nvidia.com/','gpu-lab/','hami.io/'))},
        'ownerReferences':m.get('ownerReferences',[])}


def component(obj):
    s=obj.get('spec',{});s=s.get('template',{}).get('spec',s)
    text=obj.get('metadata',{}).get('name','')+' '+' '.join(c.get('image','') for c in s.get('containers',[]))
    return bool(re.search(r'scheduler|volcano|vgpu|hami|device-plugin|dqn',text,re.I))


def spec(s,include_command=False):
    out={k:s.get(k) for k in ('nodeName','schedulerName','serviceAccountName','nodeSelector','affinity','tolerations','runtimeClassName','hostNetwork','hostPID','overhead','resourceClaims')}
    for field in ('containers','initContainers'):
        out[field]=[{k:c.get(k) for k in ('name','image','resources','securityContext','volumeMounts','restartPolicy')} for c in s.get(field,[])]
        if include_command:
            for original,dest in zip(s.get(field,[]),out[field]):
                dest.update(command=original.get('command'),args=original.get('args'))
                dest['gpu_env']=[e for e in original.get('env',[]) if e['name'] in ('NVIDIA_VISIBLE_DEVICES','CONFIG_FILE','MULTI_SCHEDULER_ENABLE','SCHEDULER_POD_NAME','SCHEDULER_NUM')]
    out['volumes']=[{k:v.get(k) for k in ('name','configMap','hostPath','emptyDir')} for v in s.get('volumes',[])]
    return out


def cluster(a):
    prefix=(['sudo','-n'] if a.sudo_kubectl else [])+['kubectl']
    if a.kubeconfig:prefix+=['--kubeconfig='+a.kubeconfig]
    def get(ident,resource,project,extra=None):
        return run(ident,prefix+['get',resource]+(extra or ['-A'])+['-o','json','--request-timeout=12s'],project)
    run('kubernetes_version',prefix+['version','-o','json','--request-timeout=12s'],lambda x:x)
    nodes=get('nodes','nodes',lambda x:[{'metadata':meta(n),'spec':n.get('spec'),
        'status':{k:n.get('status',{}).get(k) for k in ('capacity','allocatable','conditions','addresses','nodeInfo')}} for n in x['items']],[]) or []
    pods=get('pods','pods',lambda x:[{'metadata':meta(p),'spec':spec(p.get('spec',{}),component(p)),
        'status':p.get('status',{}),'component':component(p)} for p in x['items']]) or []
    controllers=get('controllers','deployments,daemonsets,statefulsets',lambda x:[{'kind':p['kind'],'metadata':meta(p),
        'replicas':p.get('spec',{}).get('replicas'),'spec':spec(p.get('spec',{}).get('template',{}).get('spec',{}),True),
        'status':p.get('status',{})} for p in x['items'] if component(p)]) or []
    for ident,res in [('queues','queues.scheduling.volcano.sh'),('podgroups','podgroups.scheduling.volcano.sh'),
                      ('runtimeclasses','runtimeclasses.node.k8s.io'),('leases','leases.coordination.k8s.io')]:
        get(ident,res,lambda x:[{'metadata':meta(p),'spec':p.get('spec'),'handler':p.get('handler'),'status':p.get('status')} for p in x['items']])
    get('admission_webhooks','mutatingwebhookconfigurations,validatingwebhookconfigurations',lambda x:[
        {'kind':p['kind'],'metadata':meta(p),'webhooks':[{k:w.get(k) for k in ('name','rules','failurePolicy','namespaceSelector','objectSelector','matchConditions')} for w in p.get('webhooks',[])]} for p in x['items']],[])
    get('admission_policies','validatingadmissionpolicies,validatingadmissionpolicybindings',lambda x:x['items'],[])
    get('scheduler_rbac','clusterroles,clusterrolebindings',lambda x:[p for p in x['items'] if any(t in p['metadata']['name'].lower() for t in ('volcano','scheduler','gpu'))],[])
    get('namespaced_rbac','roles,rolebindings',lambda x:[p for p in x['items'] if 'volcano' in p['metadata'].get('namespace','') or 'scheduler' in p['metadata']['name']])
    maps=set()
    for p in controllers+ [p for p in pods if p['component']]:
        for v in p['spec']['volumes']:
            if v.get('configMap'):maps.add((p['metadata']['namespace'],v['configMap']['name']))
    for namespace,name in sorted(maps):
        get('configmap:'+namespace+'/'+name,'configmap',lambda x:{'metadata':meta(x),'data':x.get('data',{})},[name,'-n',namespace])
    for p in [p for p in pods if p['component'] and re.search(r'scheduler|vgpu-device-plugin',p['metadata']['name'])][:12]:
        run('logs:'+p['metadata']['namespace']+'/'+p['metadata']['name'],prefix+['logs','-n',p['metadata']['namespace'],p['metadata']['name'],
             '--tail=180','--timestamps','--request-timeout=12s'])


def _wire_fields(raw):
    def number(pos):
        n=0;shift=0
        while pos<len(raw) and shift<70:
            b=raw[pos];pos+=1;n|=(b&127)<<shift
            if b<128:return n,pos
            shift+=7
        raise ValueError('invalid protobuf varint')
    pos=0
    while pos<len(raw):
        tag,pos=number(pos);field,wire=tag>>3,tag&7
        if wire==2:
            length,pos=number(pos);end=pos+length
            if end>len(raw):raise ValueError('truncated protobuf')
            yield field,raw[pos:end];pos=end
        elif wire==0:_,pos=number(pos)
        elif wire==1:pos+=8
        elif wire==5:pos+=4
        else:raise ValueError('unsupported protobuf wire')


def allocate_envs(raw):
    # kubelet/v1beta1/api.proto ContainerAllocateResponse.envs = field 1;
    # map entries have key=1/value=2. Never execute payloads or expose other envs.
    if not isinstance(raw,str):return None
    env={}
    for field,entry in _wire_fields(base64.b64decode(raw,validate=True)):
        if field!=1:continue
        values=dict(_wire_fields(entry))
        if 1 in values and 2 in values:
            key=values[1].decode()
            if key=='NVIDIA_VISIBLE_DEVICES' or key.startswith(('CUDA_DEVICE_MEMORY_LIMIT_','CUDA_DEVICE_SM_LIMIT')):
                env[key]=values[2].decode()
    return env


def checkpoint(data):
    inner=data.get('Data',data) or {}
    if isinstance(inner,str):inner=json.loads(inner)
    rows=[]
    for e in inner.get('PodDeviceEntries',[]) or []:
        if not any(s in e.get('ResourceName','') for s in ('gpu','vgpu')):continue
        row={k:e.get(k) for k in ('PodUID','ContainerName','ResourceName')}
        ids=e.get('DeviceIDs') or []
        if isinstance(ids,dict):ids=[v for values in ids.values() for v in values]
        row.update(DeviceIDs=ids[:8],device_id_count=len(ids),device_ids_truncated=len(ids)>8)
        raw=e.get('AllocResp','')
        if isinstance(raw,str):
            row['allocation_response_sha256']=hashlib.sha256(raw.encode()).hexdigest()
            try:
                row['allocate_envs']=allocate_envs(raw)
                row['consumed_uuid_source']='kubelet_checkpoint_allocate_response'
            except (ValueError,UnicodeError):row['allocate_response_decode_status']='unsupported_or_invalid; UUID not inferred'
        rows.append(row)
    registered=inner.get('RegisteredDevices') or {}
    return {'entries':rows,'registered_devices':{k:{'count':len(v),'sample':v[:8]} for k,v in registered.items()},
            'provenance':'direct kubelet checkpoint; UUID consumed evidence only when AllocateResponse.envs decoded for matching PodUID'}


def node():
    run('gpu_query',['nvidia-smi','--query-gpu=index,uuid,name,memory.total,memory.used,memory.free,utilization.gpu,driver_version','--format=csv,noheader,nounits'])
    processes=run('gpu_processes',['nvidia-smi','--query-compute-apps=gpu_uuid,pid,process_name,used_gpu_memory','--format=csv,noheader,nounits'])
    if processes:
        for pid in sorted(set(re.findall(r'GPU-[^,]+,\s*(\d+),',processes))):
            run('process_owner:'+pid,['ps','-p',pid,'-o','pid=,user=,comm='])
            run('process_cgroup:'+pid,['cat','/proc/'+pid+'/cgroup'])
    run('cri_images',['crictl','images','-o','json'],lambda x:[{k:i.get(k) for k in ('id','repoTags','repoDigests','size')} for i in x.get('images',[])],sudo=True)
    run('cri_containers',['crictl','ps','-o','json'],lambda x:[{k:i.get(k) for k in ('id','podSandboxId','metadata','labels','image','state')} for i in x.get('containers',[])],sudo=True)
    run('kubelet_checkpoint',['cat','/var/lib/kubelet/device-plugins/kubelet_internal_checkpoint'],checkpoint,sudo=True)
    root=Path('/home/zbs/rf-ablation-20261019-1425/lab')
    if root.exists():
        names=['results/linux-verification-final-checked/execution.json','results/linux-verification-final-checked/scheduler-binary.sha256',
               'results/linux-verification-final-checked/summary.json','results/linux-verification-final-checked/cleanup-verification.json',
               'models/pod-gpu-rf-v1/manifest.json']
        for name in names:
            p=root/name
            if p.is_file():
                raw=p.read_bytes();emit({'id':'round6_artifact:'+name,'sha256':hashlib.sha256(raw).hexdigest(),
                    'data':json.loads(raw) if p.suffix=='.json' else raw.decode()})
        for name in ['results/linux-verification-final-checked/vc-scheduler','scheduler/volcano-rf/pkg/scheduler/api/devices/nvidia/vgpu/rf_job.go']:
            p=root/name
            if p.exists():
                h=hashlib.sha256()
                with p.open('rb') as f:
                    for block in iter(lambda:f.read(1048576),b''):h.update(block)
                emit({'id':'round6_file:'+name,'sha256':h.hexdigest(),'size':p.stat().st_size})


def supplement(a):
    run('kubelet_checkpoint',['cat','/var/lib/kubelet/device-plugins/kubelet_internal_checkpoint'],checkpoint,sudo=True)
    cri=shutil.which('crictl')
    if cri:
        run('cri_images',[cri,'images','-o','json'],lambda x:[{k:i.get(k) for k in ('id','repoTags','repoDigests')} for i in x.get('images',[])],sudo=True)
    elif shutil.which('ctr'):
        run('ctr_cached_images',['ctr','--namespace','k8s.io','images','list'],sudo=True)
    if a.management_runtime:
        prefix=(['sudo','-n'] if a.sudo_kubectl else [])+['kubectl']
        if a.kubeconfig:prefix+=['--kubeconfig='+a.kubeconfig]
        run('node_locks',prefix+['get','nodes','-o','json','--request-timeout=10s'],lambda x:[{'node':n['metadata']['name'],'hamivgpu':n['metadata'].get('annotations',{}).get('hamivgpu')} for n in x['items']])
        pods=run('image_probe_pods',prefix+['get','pods','-n','volcano-system','-o','json','--request-timeout=10s'],lambda x:[
            {'name':p['metadata']['name'],'uid':p['metadata']['uid'],'containers':p.get('status',{}).get('containerStatuses',[])}
            for p in x['items'] if p['metadata']['name'].startswith('dqn-scheduler-grpc-')]) or []
        if cri:
            for pod in pods:
                for c in pod['containers']:
                    cid=c.get('containerID','').split('://')[-1]
                    if not cid:continue
                    r=run('image_probe_container',[cri,'inspect','-o','json',cid],lambda x:{'pid':x.get('info',{}).get('pid'),'imageRef':x.get('status',{}).get('imageRef')},sudo=True)
                    if r and r.get('pid'):
                        base='/proc/'+str(r['pid'])+'/root'
                        run('cached_sleep_ELF',['readelf','-l',base+'/bin/sleep'],sudo=True)
                        run('cached_sleep_file',['sha256sum',base+'/bin/sleep',base+'/lib/x86_64-linux-gnu/libc.so.6'],sudo=True)
        binary='/home/zbs/rf-ablation-20261019-1425/lab/results/linux-verification-final-checked/vc-scheduler'
        run('scheduler_build_info',['go','version','-m',binary])
        help_text=run('scheduler_cli_help',[binary,'--help'])


def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--cluster',action='store_true');ap.add_argument('--node',action='store_true')
    ap.add_argument('--kubeconfig');ap.add_argument('--sudo-kubectl',action='store_true')
    ap.add_argument('--supplement',action='store_true');ap.add_argument('--management-runtime',action='store_true');a=ap.parse_args()
    emit({'id':'meta','collector':'round7-readonly/v1','host':socket.gethostname(),
          'utc':dt.datetime.now(dt.timezone.utc).isoformat(),'read_only':True,'cluster_resources_changed':False})
    if a.cluster:cluster(a)
    if a.node:node()
    if a.supplement:supplement(a)


if __name__=='__main__':main()
