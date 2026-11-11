"""Bounded, explicitly authorized live single-Pod/two-vGPU acceptance.

Reuses audited round-7 transport, guard, logs and UID-preconditioned deletion.
Only sleep workloads; keeps a durable recovery journal and independent watchdog.
Run on the 4090 management host. --prepare is Kubernetes read-only.
"""
import argparse
import copy
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

ROOT=Path('/home/zbs/rf-multi-vgpu-20261028')
LAB=Path('/home/zbs/rf-round7-live-window-20261019/lab')
sys.path[:0]=[str(LAB/'scripts'),str(LAB)]
import round7_live_execute as previous
previous.ROOT=ROOT
spec=importlib.util.spec_from_file_location('audited_staging',Path('/home/zbs/rf-round8-live-20261020/helpers/staging.py'))
staging=importlib.util.module_from_spec(spec);spec.loader.exec_module(staging)
previous.inventory=staging.inventory
from round7_window_preflight import blockers
from round7_collect import checkpoint
import yaml
K=previous.K

def save(path,value):
    p=Path(path);p.parent.mkdir(parents=True,exist_ok=True)
    q=p.with_suffix(p.suffix+'.tmp');q.write_text(json.dumps(value,indent=2));q.replace(p)

class Run(previous.Run):
    def backup(self,key,value):save(ROOT/(key+'.json'),value)
    def prior(self,key):return json.loads((ROOT/(key+'.json')).read_text())
    def guard(self):
        if self.state.get('recovering'):return self.get('pods')['items']
        pods=super().guard()
        own={r['uid'] for r in self.state['pods']}
        if any(p['uid'] not in own for p in blockers(pods,'t3dgq')['finite_plugin_candidates']):
            raise RuntimeError('Foreign in-flight plugin allocation')
        return pods
    def fingerprint(self):
        model=LAB/'models/pod-gpu-rf-v1/random_forest.joblib'
        cri=json.loads(self.cmd(['sudo','-n','crictl','ps','-o','json']).stdout)
        rows=[c for c in cri['containers'] if c.get('metadata',{}).get('name')=='volcano-scheduler']
        if len(rows)!=1:raise RuntimeError('Expected one original scheduler process')
        info=json.loads(self.cmd(['sudo','-n','crictl','inspect','-o','json',rows[0]['id']]).stdout)
        return dict(model_sha256=hashlib.sha256(model.read_bytes()).hexdigest(),
            original_binary_sha256=self.cmd(['sudo','-n','sha256sum',f"/proc/{info['info']['pid']}/exe"]).stdout.split()[0])
    def prepare(self):
        if (ROOT/'prepared.json').exists():raise RuntimeError('Preserve previous preparation')
        self.guard();snap=self.snapshot('preflight')
        if snap['node_lock'] or any(g['allocated_vgpu_count'] for g in snap['devices']):raise RuntimeError('Occupied logical resources/NodeLock')
        claims=json.loads(self.cmd(K+['get','resourceclaims.resource.k8s.io','-A','-o','json']).stdout)
        if any(c.get('status',{}).get('allocation') or c.get('status',{}).get('reservedFor') for c in claims['items']):raise RuntimeError('DRA reservations')
        self.backup('dra-before',claims)
        for key,kind,name,ns in [('deployment-original','deployment','volcano-scheduler','volcano-system'),
            ('deployment-rf','deployment','volcano-rf-test','gpu-rf-system'),('configmap-original','configmap','volcano-scheduler-configmap','volcano-system'),
            ('config-rf','configmap','rf-v7-config-on','gpu-rf-system'),('quota-original','resourcequota','rf-v7-test-budget','gpu-rf-tests'),
            ('dqn-before','deployment','dqn-scheduler-grpc','volcano-system')]:self.backup(key,self.get(kind,name,ns))
        old=self.prior('deployment-original');rf=self.prior('deployment-rf')
        if old['spec']['replicas']!=1 or old['status'].get('availableReplicas')!=1 or rf['spec']['replicas']!=0 or self.scheduler_pods():raise RuntimeError('Unexpected scheduler state')
        if next(v for v in rf['spec']['template']['spec']['volumes'] if v['name']=='config')['configMap']['name']!='rf-v7-config-on':raise RuntimeError('Unexpected RF config')
        if 'rf-job' not in json.dumps(self.prior('config-rf')['data']):raise RuntimeError('RF policy not enabled')
        quota=self.prior('quota-original')
        if any(int(v) for v in quota.get('status',{}).get('used',{}).values()):raise RuntimeError('Nonempty experiment quota')
        for verb,res,ns in [('update','deployments/scale','volcano-system'),('update','deployments/scale','gpu-rf-system'),('create','pods','gpu-rf-tests'),('delete','pods','gpu-rf-tests'),('create','podgroups.scheduling.volcano.sh','gpu-rf-tests'),('delete','podgroups.scheduling.volcano.sh','gpu-rf-tests')]:
            if self.cmd(K+['auth','can-i',verb,res,'-n',ns]).stdout.strip()!='yes':raise RuntimeError('Missing permission '+res)
        template=list(yaml.safe_load_all((LAB/'k8s/round7-logical-window/test-a-training.yaml').read_text()))
        imgs=json.loads(self.cmd(['sudo','-n','crictl','images','-o','json']).stdout)
        cached={d for i in imgs['images'] for d in i.get('repoDigests',[])}
        for image in [c['image'] for c in rf['spec']['template']['spec']['containers']]+[template[1]['spec']['containers'][0]['image']]:
            if image not in cached:raise RuntimeError('Image not cached: '+image)
        for task in ['training','inference']:
            docs=copy.deepcopy(template);name='rf-multi-20261028-'+task
            for o in docs:
                o['metadata']['name']=name
                if self.get(o['kind'],name,'gpu-rf-tests',optional=True):raise RuntimeError('Name collision')
            p=docs[1];p['metadata']['labels']['gpu-lab/workload-type']=task
            p['metadata']['annotations']['scheduling.k8s.io/group-name']=name
            for field in ['requests','limits']:p['spec']['containers'][0]['resources'][field]['volcano.sh/vgpu-number']='2'
            (ROOT/(task+'.yaml')).write_text(yaml.safe_dump_all(docs,sort_keys=False))
            # Admission, quota and runtime schema validation, without persisting resources.
            self.cmd(K+['create','--dry-run=server','-f',str(ROOT/(task+'.yaml')),'-o','yaml'])
        self.backup('fingerprint-before',self.fingerprint())
        self.backup('authorization',dict(approved=True,source='User: 那你做一次真实集群多vGPU分配验收',scope='temporary scheduler replica switch; own two-vGPU sleep Pods/PodGroups; cleanup and restoration',utc=previous.now()))
        self.backup('prepared',dict(success=True,utc=previous.now()))
        print('PREPARED',flush=True)
    def evidence_multi(self,pod,tag):
        self.logs(tag);uid=pod['metadata']['uid'];ann=pod['metadata']['annotations']
        native=staging.allocations(ann.get('volcano.sh/vgpu-ids-new'))
        uuids=[d['uuid'] for d in native]
        if len(uuids)!=2 or len(set(uuids))!=2 or any((d['memory_mib'],d['compute_quota'],d['container_index'])!=(4096,20,0) for d in native):raise RuntimeError('Invalid multi-device annotation')
        cp=checkpoint(json.loads(self.cmd(['sudo','-n','cat','/var/lib/kubelet/device-plugins/kubelet_internal_checkpoint']).stdout))
        save(self.out/(tag+'-checkpoint.json'),cp)
        matching=[e for e in cp['entries'] if e['PodUID']==uid and e['ContainerName']=='sleep' and e['ResourceName']=='volcano.sh/vgpu-number']
        if len(matching)!=1:raise RuntimeError('Missing exact kubelet response')
        consumed=matching[0].get('allocate_envs',{}).get('NVIDIA_VISIBLE_DEVICES','').split(',')
        if consumed!=uuids:raise RuntimeError('Device Plugin UUID mismatch')
        commits=[s for s in (self.out/(tag+'-scheduler.log')).read_text().splitlines() if 'rf-job UUID allocation' in s and uid in s]
        if len(commits)!=1:raise RuntimeError('Missing unique native commit')
        receipt=staging.native_commit_receipt(commits[0]);advice=None
        for line in (self.out/(tag+'-rf-model.log')).read_text().splitlines():
            try:o=json.loads(line[line.index('{'):])
            except (ValueError,json.JSONDecodeError):continue
            if o.get('event')=='rf_advice' and o.get('pod',{}).get('uid')==uid and o.get('request_id')==receipt['requestID']:advice=o
        if not advice or advice['model_version']!=previous.EXPECTED_MODEL or receipt['fallback_used']:raise RuntimeError('Missing actual RF decision')
        recommended=[a['gpu_uuid'] for a in advice['assignments']]
        if recommended!=uuids:raise RuntimeError('RF/native UUID mismatch')
        result=dict(pod_name=pod['metadata']['name'],pod_uid=uid,workload_type=pod['metadata']['labels']['gpu-lab/workload-type'],
            node=pod['spec']['nodeName'],rf_uuids=recommended,allocate_uuids=uuids,plugin_uuids=consumed,
            annotation=ann['volcano.sh/vgpu-ids-new'],native_receipt=receipt,rf_advice=advice,
            plugin_evidence='PodUID/container/resource matched kubelet AllocateResponse',phase=pod['status']['phase'],cuda_executed=False)
        save(self.out/(tag+'-uuid-proof.json'),result);return result
    def submit_multi(self,task):
        before=self.snapshot(task+'-before')
        if before['node_lock']:raise RuntimeError('Stale NodeLock')
        docs=list(yaml.safe_load_all((ROOT/(task+'.yaml')).read_text()))
        self.state['planned']=docs;self.save()
        for o in docs:self.create(o)
        name=docs[1]['metadata']['name']
        def running():
            self.guard();p=self.get('pod',name,'gpu-rf-tests');save(self.out/(task+'-pod.json'),p)
            if p['status'].get('phase') in ('Failed','Succeeded'):raise RuntimeError('Unexpected terminal Pod')
            return p if p['status'].get('phase')=='Running' else False
        p=self.wait(running,120)
        self.wait(lambda:not self.get('node','t3dgq')['metadata'].get('annotations',{}).get('hamivgpu'),30)
        proof=self.evidence_multi(p,task);after=self.snapshot(task+'-after')
        ids=set(proof['allocate_uuids']);prior={d['uuid']:d for d in before['devices']}
        for g in after['devices']:
            delta=tuple(g[k]-prior[g['uuid']][k] for k in ['allocated_memory_mib','allocated_compute_quota','allocated_vgpu_count'])
            if delta!=((4096,20,1) if g['uuid'] in ids else (0,0,0)):raise RuntimeError('Wrong per-device ledger delta')
        for phase,snapshot in [('logicalBefore',before),('logicalAfter',after)]:
            direct={g['uuid']:g for g in proof['native_receipt'][phase]}
            for g in snapshot['devices']:
                for k in ['allocated_memory_mib','allocated_compute_quota','allocated_vgpu_count']:
                    if direct[g['uuid']][k]!=g[k]:raise RuntimeError('Native/API ledger mismatch')
        self.state.setdefault('proofs',[]).append(proof);self.save()
        self.delete_pod(p)
        snap=self.snapshot(task+'-released')
        if snap['devices']!=before['devices']:raise RuntimeError('Release did not restore full logical ledger')
        print(task,'VERIFIED',json.dumps(ids_to_list:=proof['allocate_uuids']),flush=True)
    def recover(self):
        with (self.out/'restore.lock').open('w') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            if self.state_path.exists():self.state=json.loads(self.state_path.read_text())
            if self.state.get('restored'):return
            self.state['recovering']=True;self.save()
            # Adopt only exact planned names with our unique name and label after an uncertain create.
            for o in self.state.get('planned',[]):
                cur=self.get(o['kind'],o['metadata']['name'],'gpu-rf-tests',optional=True)
                if cur and cur['metadata']['uid'] not in {r['uid'] for r in self.state['created']}:
                    if cur['metadata'].get('labels',{}).get('gpu-lab/experiment')!='round7-rf':raise RuntimeError('Uncertain creation collision')
                    row=dict(kind=o['kind'],name=cur['metadata']['name'],namespace='gpu-rf-tests',uid=cur['metadata']['uid'])
                    self.state['created'].append(row)
                    if o['kind']=='Pod':self.state['pods'].append(row)
                    self.save()
            self.clean_pods()
            for r in self.state['created']:
                if r['kind']!='PodGroup':continue
                pg=self.get('podgroup',r['name'],'gpu-rf-tests',optional=True)
                if pg:
                    if pg['metadata']['uid']!=r['uid']:raise RuntimeError('Replaced PodGroup')
                    self.cmd(K+['delete','--raw=/apis/scheduling.volcano.sh/v1beta1/namespaces/gpu-rf-tests/podgroups/'+r['name'],'-f','-'],dict(apiVersion='v1',kind='DeleteOptions',preconditions=dict(uid=r['uid'])))
            final=self.snapshot('final-cleanup')
            if final['node_lock'] or any(g['allocated_vgpu_count'] or g['allocated_compute_quota'] or g['allocated_memory_mib'] for g in final['devices']):raise RuntimeError('Unsafe ledger/NodeLock: preserve scene')
            if self.scheduler_pods():self.logs('final')
            self.scale_rf(0)
            old=self.prior('deployment-original');cur=self.get('deployment','volcano-scheduler','volcano-system')
            if cur['metadata']['uid']!=old['metadata']['uid'] or cur['spec'] not in [old['spec'],dict(old['spec'],replicas=0)]:raise RuntimeError('Original scheduler modified externally')
            self.cmd(K+['scale','deployment','volcano-scheduler','-n','volcano-system','--current-replicas='+str(cur['spec']['replicas']),'--replicas='+str(old['spec']['replicas'])])
            self.cmd(K+['rollout','status','deployment/volcano-scheduler','-n','volcano-system','--timeout=120s'],timeout=130)
            for key,kind,name,ns,field in [('deployment-original','deployment','volcano-scheduler','volcano-system','spec'),('deployment-rf','deployment','volcano-rf-test','gpu-rf-system','spec'),('configmap-original','configmap','volcano-scheduler-configmap','volcano-system','data'),('config-rf','configmap','rf-v7-config-on','gpu-rf-system','data'),('quota-original','resourcequota','rf-v7-test-budget','gpu-rf-tests','spec'),('dqn-before','deployment','dqn-scheduler-grpc','volcano-system','spec')]:
                if self.get(kind,name,ns)[field]!=self.prior(key)[field]:raise RuntimeError('Restoration mismatch '+key)
            if self.fingerprint()!=self.prior('fingerprint-before'):raise RuntimeError('Model/original binary changed')
            self.state.update(restored=True,restored_utc=previous.now());self.save()
            save(self.out/'restore-confirmation.json',dict(original_scheduler_available=True,rf_replicas=0,own_pods_remaining=0,logical_ledger_restored=True,node_lock=None,quota_config_dqn_and_model_binary_unchanged=True))
            print('RESTORED',flush=True)
    def execute(self):
        if not self.prior('authorization')['approved'] or not self.prior('prepared')['success']:raise RuntimeError('Not authorized/prepared')
        if self.state.get('old_stopped') or self.state.get('restored'):raise RuntimeError('Do not overwrite prior execution')
        self.guard();self.state['started_epoch']=time.time();self.save()
        with (self.out/'watchdog.log').open('a') as log:
            p=subprocess.Popen([sys.executable,__file__,'--watchdog',str(os.getpid())],stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
        self.state['watchdog_pid']=p.pid;self.save()
        try:
            if self.get('deployment','volcano-scheduler','volcano-system')['spec']!=self.prior('deployment-original')['spec']:raise RuntimeError('Original spec changed')
            self.cmd(K+['scale','deployment','volcano-scheduler','-n','volcano-system','--current-replicas=1','--replicas=0'])
            self.state['old_stopped']=True;self.save()
            self.wait(lambda:not any(p['metadata']['namespace']=='volcano-system' and p['metadata']['name'].startswith('volcano-scheduler-') for p in self.get('pods')['items']),90)
            self.guard();snap=self.snapshot('original-exited')
            if snap['node_lock'] or any(g['allocated_vgpu_count'] for g in snap['devices']):raise RuntimeError('In-flight ledger after original exit')
            self.scale_rf(1)
            for task in ['training','inference']:self.submit_multi(task)
            self.state.update(experiment_success=True,finished=True);self.save()
        except BaseException as exc:
            self.state.update(experiment_success=False,error=repr(exc));self.save()
            try:self.logs('failure')
            except Exception:pass
            raise
        finally:self.recover()

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--prepare',action='store_true');ap.add_argument('--execute',action='store_true');ap.add_argument('--recover',action='store_true');ap.add_argument('--watchdog',type=int);a=ap.parse_args()
    ROOT.mkdir(exist_ok=True)
    if a.watchdog:
        start=time.monotonic()
        while time.monotonic()-start<600:
            time.sleep(5);r=Run()
            if r.state.get('restored'):return
            try:os.kill(a.watchdog,0)
            except ProcessLookupError:break
        Run().recover();return
    r=Run()
    if a.prepare:r.prepare()
    elif a.execute:
        def terminated(signum,frame):raise RuntimeError('Execution interrupted '+str(signum))
        signal.signal(signal.SIGTERM,terminated);r.execute()
    elif a.recover:r.recover()
    else:ap.error('choose action')

if __name__=='__main__':main()
