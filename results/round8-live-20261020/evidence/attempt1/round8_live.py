"""Authorized round-8 real sleep experiment, reusing round-7 evidence helpers.

Run on t3dgq. No model training, image rebuilding, DQN config edits or GPU compute.
Prepare is read-only to Kubernetes. Execute requires the durable explicit scope.
"""
import argparse
import datetime as dt
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

ROOT=Path(os.environ.get('GPU_LAB_ROUND8_RUN_ROOT','/home/zbs/rf-round8-live-20261020'))
LAB=Path('/home/zbs/rf-round7-live-window-20261019/lab')
sys.path[:0]=[str(LAB),str(LAB/'scripts')]
import round7_live_execute as previous
previous.ROOT=ROOT
previous.LAB=LAB
_spec=importlib.util.spec_from_file_location('round8_staging',ROOT/'helpers/staging.py')
_staging=importlib.util.module_from_spec(_spec);_spec.loader.exec_module(_staging)
inventory,native_commit_receipt=_staging.inventory,_staging.native_commit_receipt
previous.inventory=inventory
from round7_window_preflight import gpu_demand,blockers
from round7_collect import checkpoint
import yaml

K=previous.K
MODEL_SHA='ee35cfabe30fffc28ff0be09d3738587d4f9bffbe26386e2a9fae5c5b9e80748'
OLD_SHA='9f2e1ae3359ce234fd048bc48aab082add99a686631808908b5ae518bdebf4f4'
RF_SHA='501e9ece8f838fc2abcc82f5ea731e399178705f7a0bda0d6c02c8eac6b0d7e9'
EXPECTED_LABELS=['balanced','training_preferred','unavailable','inference_preferred','balanced']
PEAK={'pods':'6','requests.volcano.sh/vgpu-number':'6','requests.volcano.sh/vgpu-memory':'18820','requests.volcano.sh/vgpu-cores':'366'}


def now():return dt.datetime.now(dt.timezone.utc).isoformat()


def save(path,obj):
    p=Path(path);p.parent.mkdir(parents=True,exist_ok=True)
    tmp=p.with_suffix(p.suffix+'.tmp');tmp.write_text(json.dumps(obj,indent=2));tmp.replace(p)


class Run(previous.Run):
    def guard(self):
        if not self.state.get('recovering') and (ROOT/'STOP').exists():raise RuntimeError('Operator STOP')
        pods=super().guard() if not self.state.get('recovering') else self.get('pods')['items']
        if not self.state.get('recovering'):
            node=self.get('node','t3dgq')
            pending=blockers(pods,'t3dgq')['finite_plugin_candidates']
            own={r['uid'] for r in self.state['pods']}
            if any(p['uid'] not in own for p in pending):raise RuntimeError('Foreign pending Allocate')
            for p in pods:
                if p['metadata']['uid'] in own and p['metadata']['name'].startswith('rf-v8-blocker'):
                    if p.get('status',{}).get('phase') in ('Succeeded','Failed') or p['metadata'].get('deletionTimestamp'):
                        raise RuntimeError('Blocker ended unexpectedly')
            if self.state.get('started_epoch') and time.time()-self.state['started_epoch']>500:
                raise RuntimeError('Bounded execution window exceeded')
        return pods

    def backup(self,name,obj):save(ROOT/(name+'.json'),obj)

    def get_backup(self,name):return json.loads((ROOT/(name+'.json')).read_text())

    def hashes(self):
        bundle=LAB/'models/pod-gpu-rf-v1'
        model={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in bundle.iterdir() if p.is_file()}
        if model['random_forest.joblib']!=MODEL_SHA:raise RuntimeError('Frozen model SHA changed')
        cri=json.loads(self.cmd(['sudo','-n','crictl','ps','-o','json']).stdout)
        old=[c for c in cri.get('containers',[]) if c.get('metadata',{}).get('name')=='volcano-scheduler']
        executable=None
        if old:
            if len(old)!=1:raise RuntimeError('Multiple original scheduler containers')
            info=json.loads(self.cmd(['sudo','-n','crictl','inspect','-o','json',old[0]['id']]).stdout)
            pid=info['info']['pid']
            executable=self.cmd(['sudo','-n','sha256sum',f'/proc/{pid}/exe']).stdout.split()[0]
            if executable!=OLD_SHA:raise RuntimeError('Original scheduler executable differs from prior audited binary')
        source=Path('/home/zbs/volcano-v1.13.1-dqn')
        dqn={str(p.relative_to(source)):hashlib.sha256(p.read_bytes()).hexdigest()
             for p in [source/'pkg/scheduler/api/devices/nvidia/vgpu/dqn_client.go']}
        return dict(model_bundle=model,original_executable=executable,dqn_source=dqn)

    def prepare(self):
        if (ROOT/'prepared.json').exists():raise RuntimeError('Prepare already completed; preserve existing backups')
        pods=self.guard();node=self.get('node','t3dgq')
        b=blockers(pods,'t3dgq')
        if b['active_gpu_or_DRA_demand'] or b['finite_plugin_candidates'] or node['metadata'].get('annotations',{}).get('hamivgpu'):
            raise RuntimeError('GPU/DRA demand or pending allocations/NodeLock')
        for name,kind,ns in [('deployment-original','deployment','volcano-system'),('deployment-rf','deployment','gpu-rf-system'),
                             ('quota-original','resourcequota','gpu-rf-tests'),('configmap-original','configmap','volcano-system')]:
            actual={'deployment-original':'volcano-scheduler','deployment-rf':'volcano-rf-test',
                    'quota-original':'rf-v7-test-budget','configmap-original':'volcano-scheduler-configmap'}[name]
            self.backup(name,self.get(kind,actual,ns))
        original=self.get_backup('deployment-original');rf=self.get_backup('deployment-rf')
        if original['spec']['replicas']!=1 or original['status'].get('availableReplicas')!=1:raise RuntimeError('Original scheduler not 1/1')
        if rf['spec']['replicas']!=0 or self.scheduler_pods():raise RuntimeError('RF scheduler already active')
        volumes=rf['spec']['template']['spec']['volumes']
        if next(v['configMap']['name'] for v in volumes if v['name']=='config')!='rf-v7-config-on':raise RuntimeError('RF deployment not in frozen on configuration')
        config=self.get('configmap','rf-v7-config-on','gpu-rf-system');self.backup('rf-config',config)
        text=json.dumps(config['data'])
        for term in ['rf-job','rf-v4-known-states-ee35cfabe30f']:
            if term not in text:raise RuntimeError('RF configuration missing '+term)
        if not ('RFPredictionEnabled' in text and 'RFEnable' in text):raise RuntimeError('Missing RF enable flags')
        devices,warnings=inventory([node],pods)
        if warnings or len(devices)!=4 or any(d['allocated_vgpu_count'] or d['registered_memory_mib']!=24564 or not d['healthy'] or '4090' not in d['model'] for d in devices):
            raise RuntimeError('Unexpected 4090 logical inventory')
        self.backup('node-before',node);self.backup('pods-before',{'items':pods});self.backup('logical-before',devices)
        claims=json.loads(self.cmd(K+['get','resourceclaims.resource.k8s.io','-A','-o','json','--request-timeout=15s']).stdout)['items']
        if any(c.get('status',{}).get('allocation') or c.get('status',{}).get('reservedFor') for c in claims):raise RuntimeError('Allocated/reserved DRA claims')
        self.backup('dra-claims',claims)
        for verb,resource,ns in [('update','deployments/scale','volcano-system'),('update','deployments/scale','gpu-rf-system'),
                                 ('patch','resourcequotas','gpu-rf-tests'),('create','pods','gpu-rf-tests'),('delete','pods','gpu-rf-tests'),
                                 ('create','podgroups.scheduling.volcano.sh','gpu-rf-tests'),('delete','podgroups.scheduling.volcano.sh','gpu-rf-tests')]:
            answer=self.cmd(K+['auth','can-i',verb,resource,'-n',ns,'--request-timeout=15s']).stdout.strip()
            if answer!='yes':raise RuntimeError('Insufficient Kubernetes permission: '+resource)
        images=[c['image'] for c in rf['spec']['template']['spec']['containers']]
        for name in ['compute','memory','observer-0','observer-1','observer-2','blocker-0','blocker-1','blocker-2']:
            docs=list(yaml.safe_load_all((ROOT/'manifests'/(name+'.yaml')).read_text()))
            p=next(d for d in docs if d['kind']=='Pod')
            if self.get('pod',p['metadata']['name'],'gpu-rf-tests',optional=True):raise RuntimeError('Experiment Pod name already exists')
            if self.get('podgroup',p['metadata']['name'],'gpu-rf-tests',optional=True):raise RuntimeError('Experiment PodGroup already exists')
            c=p['spec']['containers'][0]
            if c['command']!=['/bin/sleep','600'] or c['imagePullPolicy']!='Never':raise RuntimeError('Unexpected workload')
            images.append(c['image'])
        image_info={}
        for image in sorted(set(images)):
            if '@sha256:' not in image:raise RuntimeError('Unpinned image')
            image_info[image]=json.loads(self.cmd(['sudo','-n','crictl','inspecti','-o','json',image]).stdout)['status']['id']
        plugin=[p for p in pods if p['spec'].get('nodeName')=='t3dgq' and any('volcano-vgpu-device-plugin' in c['image'] for c in p['spec']['containers'])]
        if len(plugin)!=1 or not all(c.get('ready') for c in plugin[0]['status'].get('containerStatuses',[])):
            raise RuntimeError('Device Plugin not healthy')
        if not any(c['type']=='Ready' and c['status']=='True' for c in node['status']['conditions']):raise RuntimeError('Node not Ready')
        runtime=self.get('runtimeclass','nvidia')
        if runtime['handler']!='nvidia':raise RuntimeError('Unexpected NVIDIA runtime handler')
        self.backup('runtimeclass-before',runtime)
        controllers=json.loads((ROOT/'gpu-controller-check.json').read_text())
        if controllers:raise RuntimeError('GPU controller templates require reconciliation')
        self.backup('plugin-before',plugin[0]);self.backup('dqn-service-before',self.get('deployment','dqn-scheduler-grpc','volcano-system'))
        self.backup('checkpoint-before',checkpoint(json.loads(self.cmd(['sudo','-n','cat','/var/lib/kubelet/device-plugins/kubelet_internal_checkpoint']).stdout)))
        self.logs_original('original-recent')
        h=self.hashes();self.backup('hashes-before',h)
        save(ROOT/'prepared.json',dict(utc=now(),success=True,devices=devices,images=image_info,permissions_confirmed=True,
             admission_boundary='explicit current user-authorized bounded GPU window; API watches and original scheduler stopped; not enforced admission isolation',
             original_private_cache='not directly dumped; reconcile API/plugin and require original process exit before RF start'))
        print(json.dumps(dict(prepared=True,root=str(ROOT),gpus=[d['uuid'] for d in devices],images=image_info)),flush=True)

    def logs_original(self,name):
        p=self.cmd(K+['logs','-n','volcano-system','deployment/volcano-scheduler','--since=120s','--limit-bytes=4000000','--timestamps','--request-timeout=15s'])
        (self.out/(name+'.log')).write_text(p.stdout)

    def submit(self,file,name):
        before=self.snapshot(name+'-before')
        if before['node_lock']:raise RuntimeError('NodeLock before submission')
        docs=list(yaml.safe_load_all((ROOT/'manifests'/file).read_text()))
        for obj in docs:
            self.state.setdefault('planned',[]).append(dict(kind=obj['kind'],name=obj['metadata']['name'],namespace=obj['metadata'].get('namespace')))
            self.save();self.create(obj)
        pod_template=next(o for o in docs if o['kind']=='Pod');pod_name=pod_template['metadata']['name']
        def running():
            self.guard();p=self.get('pod',pod_name,'gpu-rf-tests')
            save(self.out/(name+'-pod.json'),p)
            if p['status'].get('phase') in ['Failed','Succeeded']:raise RuntimeError('Unexpected terminal Pod')
            return p if p['status'].get('phase')=='Running' else False
        p=self.wait(running,60)
        self.wait(lambda:not self.get('node','t3dgq')['metadata'].get('annotations',{}).get('hamivgpu'),25)
        proof=self.evidence(p,name,True);after=self.snapshot(name+'-after')
        target=proof['native_permanent_uuids'][0]
        expected=self.state.get('target_uuid')
        if expected and target!=expected:raise RuntimeError('Allocated UUID differs from unique target')
        req=pod_template['spec']['containers'][0]['resources']['limits'];mem=int(req['volcano.sh/vgpu-memory']);core=int(req['volcano.sh/vgpu-cores'])
        old={d['uuid']:d for d in before['devices']}
        for d in after['devices']:
            delta=tuple(d[k]-old[d['uuid']][k] for k in ['allocated_memory_mib','allocated_compute_quota','allocated_vgpu_count'])
            if delta!=((mem,core,1) if d['uuid']==target else (0,0,0)):raise RuntimeError('Quota delta mismatch')
        inputs={d['uuid']:d for d in proof['rf_advice']['gpus']}
        for uuid,d in old.items():
            for key in ['allocated_memory_mib','allocated_compute_quota','allocated_vgpu_count']:
                if inputs[uuid][key]!=d[key]:raise RuntimeError('RF snapshot differs from current logical state')
        receipt=native_commit_receipt(proof['native_commit_log'])
        for phase,actual in [('logicalBefore',before),('logicalAfter',after)]:
            native={g['uuid']:g for g in receipt[phase]}
            for d in actual['devices']:
                for key in ['allocated_memory_mib','allocated_compute_quota','allocated_vgpu_count']:
                    if native[d['uuid']][key]!=d[key]:raise RuntimeError('Native/API ledger disagreement')
        return p,proof

    def record_state(self,state,proof,expected_resources):
        target=self.state['target_uuid'];advice=proof['rf_advice']
        g=next(g for g in advice['gpus'] if g['uuid']==target)
        c=next(c for c in advice['classifications'] if c['gpu_uuid']==target)
        actual=(g['registered_memory_mib']-g['allocated_memory_mib'],100-g['allocated_compute_quota'],g['allocated_vgpu_count'])
        if actual!=expected_resources:raise RuntimeError('Wrong observed state '+state+str(actual))
        result=dict(state=state,utc=now(),gpu_uuid=target,free_memory_mib=actual[0],free_compute_quota=actual[1],allocated_vgpu_count=actual[2],
            pod_uid=proof['pod_uid'],pod_name=proof['pod_name'],task_type=proof['workload_type'],
            classification=c,rf_uuid=proof['rf_recommended_uuids'][0],allocate_uuid=proof['native_permanent_uuids'][0],
            plugin_uuid=proof['device_plugin_consumed_uuids'][0],request_id=advice['request_id'],
            evidence='direct actual RF input/output correlated to native Allocate and kubelet AllocateResponse; pre-observer allocation state')
        self.state.setdefault('states',[]).append(result);self.save();save(self.out/(state+'.json'),result)
        self.event('dynamic_state',state=state,uuid=target,resources=actual,label=c['classification']['label'])

    def delete_pod(self,pod):
        name=pod['metadata']['name'];uid=pod['metadata']['uid']
        current=self.get('pod',name,'gpu-rf-tests',optional=True)
        if not current:return
        if current['metadata']['uid']!=uid or current['metadata'].get('labels',{}).get('gpu-lab/experiment')!='round8-dynamic':raise RuntimeError('Refuse replaced or foreign Pod')
        self.cmd(K+['delete','--raw=/api/v1/namespaces/gpu-rf-tests/pods/'+name,'-f','-'],
            dict(apiVersion='v1',kind='DeleteOptions',preconditions=dict(uid=uid),gracePeriodSeconds=5))
        self.wait(lambda:not self.get('pod',name,'gpu-rf-tests',optional=True),45)
        self.wait(lambda:not self.get('node','t3dgq')['metadata'].get('annotations',{}).get('hamivgpu'),25)
        self.logs('released-'+uid)
        text=(self.out/('released-'+uid+'-scheduler.log')).read_text()
        if not any('rf-job logical release' in line and uid in line for line in text.splitlines()):
            self.wait(lambda:self.release_seen(uid),15)
        self.event('pod_released',uid=uid,name=name)

    def release_seen(self,uid):
        self.logs('released-'+uid)
        return any('rf-job logical release' in line and uid in line for line in (self.out/('released-'+uid+'-scheduler.log')).read_text().splitlines())

    def adopt_uncertain_creates(self):
        known={r['uid'] for r in self.state['created']}
        for r in self.state.get('planned',[]):
            obj=self.get(r['kind'],r['name'],r['namespace'],optional=True)
            if obj and obj['metadata']['uid'] not in known:
                if obj['metadata'].get('labels',{}).get('gpu-lab/experiment')!='round8-dynamic':raise RuntimeError('Uncertain create collided with foreign object')
                row={**r,'uid':obj['metadata']['uid']};self.state['created'].append(row)
                if r['kind']=='Pod':self.state['pods'].append(row)
                known.add(row['uid']);self.save()

    def recover(self):
        with (self.out/'restore.lock').open('w') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            if self.state_path.exists():self.state=json.loads(self.state_path.read_text())
            if self.state.get('restored'):return
            self.state['recovering']=True;self.save();errors=[]
            try:self.adopt_uncertain_creates()
            except Exception as exc:errors.append('adopt: '+str(exc))
            for r in list(self.state['pods']):
                try:
                    p=self.get('pod',r['name'],'gpu-rf-tests',optional=True)
                    if p:
                        if p['metadata']['uid']!=r['uid']:raise RuntimeError('Recorded Pod UID changed; refuse deletion')
                        self.delete_pod(p)
                except Exception as exc:errors.append('cleanup '+r['name']+': '+str(exc))
            remaining=[p for p in self.get('pods')['items'] if p['metadata']['uid'] in {r['uid'] for r in self.state['pods']}]
            if remaining:raise RuntimeError('Own Pods remain; preserving RF for ledger reconciliation: '+str(errors))
            for r in self.state['created']:
                if r['kind']!='PodGroup':continue
                obj=self.get('podgroup',r['name'],'gpu-rf-tests',optional=True)
                if obj:
                    if obj['metadata']['uid']!=r['uid']:raise RuntimeError('Replaced PodGroup')
                    self.cmd(K+['delete','--raw=/apis/scheduling.volcano.sh/v1beta1/namespaces/gpu-rf-tests/podgroups/'+r['name'],'-f','-'],
                        dict(apiVersion='v1',kind='DeleteOptions',preconditions=dict(uid=r['uid'])))
            final=self.snapshot('cleanup-logical-zero')
            if any(d['allocated_vgpu_count'] or d['allocated_memory_mib'] or d['allocated_compute_quota'] for d in final['devices']) or final['node_lock']:
                raise RuntimeError('Logical state/NodeLock not clear; refuse blind restoration')
            if self.scheduler_pods():self.logs('final-before-rf-stop')
            self.scale_rf(0)
            quota=self.get_backup('quota-original');current=self.get('resourcequota','rf-v7-test-budget','gpu-rf-tests')
            if current['metadata']['uid']!=quota['metadata']['uid']:raise RuntimeError('Quota UID changed')
            if current['spec']!=quota['spec']:
                if current['spec']!={**quota['spec'],'hard':PEAK}:raise RuntimeError('Quota has unexplained external changes')
                self.cmd(K+['patch','resourcequota','rf-v7-test-budget','-n','gpu-rf-tests','--type=json','-p',json.dumps([
                    {'op':'test','path':'/metadata/resourceVersion','value':current['metadata']['resourceVersion']},
                    {'op':'replace','path':'/spec','value':quota['spec']}]),'--request-timeout=20s'])
            old=self.get_backup('deployment-original');cur=self.get('deployment','volcano-scheduler','volcano-system')
            expected={**old['spec'],'replicas':0}
            if cur['metadata']['uid']!=old['metadata']['uid'] or cur['spec'] not in (old['spec'],expected):raise RuntimeError('Original deployment changed unexpectedly')
            if self.scheduler_pods():raise RuntimeError('RF still active')
            self.cmd(K+['scale','deployment','volcano-scheduler','-n','volcano-system','--current-replicas='+str(cur['spec']['replicas']),
                        '--replicas='+str(old['spec']['replicas']),'--request-timeout=20s'])
            self.cmd(K+['rollout','status','deployment/volcano-scheduler','-n','volcano-system','--timeout=90s'],timeout=100)
            restored=self.get('deployment','volcano-scheduler','volcano-system')
            config=self.get('configmap','volcano-scheduler-configmap','volcano-system')
            rf=self.get('deployment','volcano-rf-test','gpu-rf-system');q=self.get('resourcequota','rf-v7-test-budget','gpu-rf-tests')
            h=self.hashes();prior=self.get_backup('hashes-before')
            dqn=self.get('deployment','dqn-scheduler-grpc','volcano-system')
            if restored['spec']!=old['spec'] or config['data']!=self.get_backup('configmap-original')['data'] or q['spec']!=quota['spec']:
                raise RuntimeError('Restoration data not identical')
            if rf['spec']!=self.get_backup('deployment-rf')['spec'] or h!=prior or dqn['spec']!=self.get_backup('dqn-service-before')['spec']:
                raise RuntimeError('Unexpected RF/DQN/model/binary change')
            self.state.update(restored=True,restored_utc=now(),cleanup_errors=errors);self.save()
            save(self.out/'restore-confirmation.json',dict(utc=now(),original_replicas=restored['spec']['replicas'],original_available=restored['status'].get('availableReplicas'),
                original_spec_equal=True,original_config_equal=True,original_config_rv=config['metadata']['resourceVersion'],quota_restored=True,
                rf_replicas=rf['spec']['replicas'],rf_deployment_spec_equal=True,dqn_service_spec_equal=True,model_and_binary_hashes_equal=True,
                hashes_after=h,own_pods_remaining=0,node_lock=None,cleanup_errors=errors))
            self.event('restored',errors=errors)

    def execute(self):
        approved=json.loads((ROOT/'authorization.json').read_text())
        if not approved.get('approved') or approved.get('authorization_id')!='user-round8-live-20261020':raise RuntimeError('Missing current explicit authorization')
        if not self.get_backup('prepared')['success']:raise RuntimeError('Not prepared')
        if self.state.get('old_stopped') or self.state.get('restored'):raise RuntimeError('Preserve prior attempt; do not rerun')
        self.guard();self.state.update(started_epoch=time.time(),started_utc=now());self.save()
        watchdog_log=(self.out/'watchdog.log').open('a')
        watcher=subprocess.Popen([sys.executable,__file__,'--watchdog',str(os.getpid())],stdin=subprocess.DEVNULL,
            stdout=watchdog_log,stderr=subprocess.STDOUT,start_new_session=True)
        self.state['watchdog_pid']=watcher.pid;self.save()
        try:
            old=self.get_backup('deployment-original');quota=self.get_backup('quota-original')
            if self.get('deployment','volcano-scheduler','volcano-system')['spec']!=old['spec']:raise RuntimeError('Original spec changed since backup')
            cur=self.get('resourcequota','rf-v7-test-budget','gpu-rf-tests')
            if cur['spec']!=quota['spec']:raise RuntimeError('Quota changed since backup')
            self.cmd(K+['patch','resourcequota','rf-v7-test-budget','-n','gpu-rf-tests','--type=json','-p',json.dumps([
                {'op':'test','path':'/metadata/resourceVersion','value':cur['metadata']['resourceVersion']},
                {'op':'replace','path':'/spec/hard','value':PEAK}]),'--request-timeout=20s'])
            self.cmd(K+['scale','deployment','volcano-scheduler','-n','volcano-system','--current-replicas=1','--replicas=0','--request-timeout=20s'])
            self.state.update(old_stopped=True,stopped_utc=now());self.save()
            self.wait(lambda:not any(p['metadata']['namespace']=='volcano-system' and p['metadata']['name'].startswith('volcano-scheduler-') for p in self.get('pods')['items']),60)
            cri=json.loads(self.cmd(['sudo','-n','crictl','ps','-o','json']).stdout)
            if any(c.get('metadata',{}).get('name')=='volcano-scheduler' for c in cri.get('containers',[])):raise RuntimeError('Original process still alive')
            snap=self.snapshot('after-original-exited')
            if any(d['allocated_vgpu_count'] for d in snap['devices']) or snap['node_lock']:raise RuntimeError('In-flight allocation after original exit')
            self.scale_rf(1);self.logs('startup')
            occupied=[]
            for i in range(3):
                p,proof=self.submit(f'blocker-{i}.yaml',f'blocker-{i}')
                occupied.append(proof['native_permanent_uuids'][0])
                if len(set(occupied))!=len(occupied):raise RuntimeError('Blocker UUID repeated')
            snap=self.snapshot('three-blockers')
            remaining=[d for d in snap['devices'] if d['free_compute_quota']==100 and d['allocated_vgpu_count']==0]
            if len(remaining)!=1:raise RuntimeError('No unique target')
            self.state['target_uuid']=remaining[0]['uuid'];self.save()
            a,pa=self.submit('compute.yaml','compute');self.record_state('S0',pa,(24564,100,0))
            b,pb=self.submit('memory.yaml','memory');self.record_state('S1',pb,(23564,40,1))
            o,po=self.submit('observer-0.yaml','observer-0');self.record_state('S2',po,(6000,35,2));self.delete_pod(o)
            self.delete_pod(a);self.snapshot('after-compute-release')
            o,po=self.submit('observer-1.yaml','observer-1');self.record_state('S3',po,(7000,95,1));self.delete_pod(o)
            self.delete_pod(b);self.snapshot('after-memory-release')
            o,po=self.submit('observer-2.yaml','observer-2');self.record_state('S4',po,(24564,100,0));self.delete_pod(o)
            actual=[s['classification']['classification']['label'] for s in self.state['states']]
            self.state.update(experiment_success=actual==EXPECTED_LABELS,actual_labels=actual,finished=True);self.save()
            if actual!=EXPECTED_LABELS:raise RuntimeError('Observed classification differs; outputs preserved')
        except BaseException as exc:
            self.state.update(experiment_success=False,error=repr(exc));self.save();self.event('experiment_error',error=repr(exc))
            try:self.logs('failure')
            except Exception:pass
            raise
        finally:self.recover()


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--prepare',action='store_true');ap.add_argument('--execute',action='store_true')
    ap.add_argument('--recover',action='store_true');ap.add_argument('--watchdog',type=int);a=ap.parse_args()
    ROOT.mkdir(parents=True,exist_ok=True)
    if a.prepare:Run().prepare();return
    if a.watchdog:
        started=time.monotonic()
        while time.monotonic()-started<660:
            time.sleep(5);r=Run()
            if r.state.get('restored'):return
            try:os.kill(a.watchdog,0)
            except ProcessLookupError:break
            hb=r.out/'heartbeat'
            if hb.exists() and time.time()-hb.stat().st_mtime>120:
                (ROOT/'STOP').touch();os.kill(a.watchdog,signal.SIGTERM);break
        with (ROOT/'run.lock').open('w') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX);Run().recover()
        return
    with (ROOT/'run.lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        def stop(sig,frame):raise RuntimeError('Execution interrupted by signal '+str(sig))
        signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
        r=Run()
        if a.recover:r.recover()
        elif a.execute:r.execute()
        else:ap.error('Choose prepare/execute/recover')


if __name__=='__main__':main()
