"""Authorized cross-node sleep acceptance using the existing frozen RF deployment.

Run on 4090. Windows collector supplies target-node checkpoint via strict-key SSH.
No GPU computation; all original scheduler/RF/RBAC settings restored in finally.
"""
import argparse
import copy
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

spec=importlib.util.spec_from_file_location('multi', '/home/zbs/rf-multi-vgpu-20261028/multi_vgpu_live.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
ROOT=Path('/home/zbs/rf-cross-node-20261028');m.ROOT=ROOT;m.previous.ROOT=ROOT
K=m.K;save=m.save;yaml=m.yaml
NODES=['t3dgq','t3dmg','t3dgq-2']
CASES=[('training','training',None),('inference','inference',None),('5090-path','inference','t3dmg'),('l40-path','training','t3dgq-2')]

class Run(m.Run):
    def snapshot(self,name):
        pods=self.guard();nodes=[self.get('node',n) for n in NODES]
        devices,warnings=m.staging.inventory(nodes,pods)
        if warnings or len(devices)!=11:raise RuntimeError('Unreconciled 11-GPU inventory '+str(warnings))
        for g in devices:
            if not g['healthy'] or not g['node_ready'] or min(g['free_memory_mib'],g['free_compute_quota'],g['free_instances'])<0:raise RuntimeError('Unhealthy/oversubscribed GPU')
        locks={n['metadata']['name']:n['metadata'].get('annotations',{}).get('hamivgpu') for n in nodes}
        result=dict(utc=m.previous.now(),devices=devices,node_lock=next((v for v in locks.values() if v),None),node_locks=locks,source='active Pod annotation reconstruction; native commit evidence is separate')
        save(self.out/(name+'-ledger.json'),result);return result
    def guard(self):
        pods=super().guard()
        if not self.state.get('recovering'):
            own={r['uid'] for r in self.state['pods']}
            for node in NODES:
                if any(p['uid'] not in own for p in m.blockers(pods,node)['finite_plugin_candidates']):raise RuntimeError('Foreign in-flight allocation')
        return pods
    def prepare(self):
        super().prepare()
        self.backup('rbac-original',self.get('clusterrole','rf-v7-scheduler'))
        for n in NODES:
            node=self.get('node',n)
            plugin=[p for p in self.get('pods')['items'] if p['spec'].get('nodeName')==n and p['metadata']['name'].startswith('volcano-device-plugin-')]
            if len(plugin)!=1 or not all(c['ready'] for c in plugin[0]['status'].get('containerStatuses',[])):raise RuntimeError('Plugin not ready '+n)
            self.backup('plugin-'+n,plugin[0])
        docs=list(yaml.safe_load_all((ROOT/'training.yaml').read_text()))
        for case,task,node in CASES:
            objs=copy.deepcopy(docs);name='rf-cross-20261028-'+case
            for o in objs:o['metadata']['name']=name
            p=objs[1];p['metadata']['annotations']['scheduling.k8s.io/group-name']=name
            p['metadata']['labels']['gpu-lab/workload-type']=task
            p['metadata']['labels']['gpu-lab/case']=case
            p['spec'].pop('nodeSelector',None)
            p['spec']['affinity']={'nodeAffinity':{'requiredDuringSchedulingIgnoredDuringExecution':{'nodeSelectorTerms':[{'matchExpressions':[{'key':'kubernetes.io/hostname','operator':'In','values':[node] if node else NODES}]}]}}}
            c=p['spec']['containers'][0];c['image']='docker.io/projecthami/volcano-vgpu-device-plugin:v1.11.0'
            c['env']=[{'name':'NVIDIA_DRIVER_CAPABILITIES','value':'utility'}]
            for f in ['requests','limits']:c['resources'][f]['volcano.sh/vgpu-number']='1'
            (ROOT/(case+'.yaml')).write_text(yaml.safe_dump_all(objs,sort_keys=False))
            self.cmd(K+['create','--dry-run=server','-f',str(ROOT/(case+'.yaml')),'-o','yaml'])
        self.backup('authorization',dict(approved=True,source='User: 可以做跨节点啊',scope='4090/5090/L40 sleep selection; temporary RF selector and node RBAC expansion; original scheduler scale and restore',utc=m.previous.now()))
        self.backup('cross-prepared',dict(success=True))
        print('CROSS PREPARED',flush=True)
    def evidence(self,pod,case):
        self.logs(case);uid=pod['metadata']['uid'];ann=pod['metadata']['annotations']
        native=m.staging.allocations(ann.get('volcano.sh/vgpu-ids-new'));ids=[d['uuid'] for d in native]
        if len(ids)!=1 or (native[0]['memory_mib'],native[0]['compute_quota'])!=(4096,20):raise RuntimeError('Invalid annotation')
        save(ROOT/'checkpoint-request.json',dict(task=case,node=pod['spec']['nodeName'],pod_uid=uid))
        path=ROOT/(case+'-raw-checkpoint.json')
        self.wait(lambda:path.exists(),150)
        cp=m.checkpoint(json.loads(path.read_text()));save(self.out/(case+'-checkpoint.json'),cp)
        matches=[e for e in cp['entries'] if e['PodUID']==uid and e['ContainerName']=='sleep' and e['ResourceName']=='volcano.sh/vgpu-number']
        if len(matches)!=1:raise RuntimeError('Missing target-node kubelet response')
        consumed=matches[0].get('allocate_envs',{}).get('NVIDIA_VISIBLE_DEVICES','').split(',')
        if consumed!=ids:raise RuntimeError('Plugin UUID mismatch')
        commits=[s for s in (self.out/(case+'-scheduler.log')).read_text().splitlines() if 'rf-job UUID allocation' in s and uid in s]
        if len(commits)!=1:raise RuntimeError('Missing native commit')
        receipt=m.staging.native_commit_receipt(commits[0]);advices=[]
        for line in (self.out/(case+'-rf-model.log')).read_text().splitlines():
            try:o=json.loads(line[line.index('{'):])
            except (ValueError,json.JSONDecodeError):continue
            if o.get('event')=='rf_advice' and o.get('pod',{}).get('uid')==uid:advices.append(o)
        advice=next((a for a in advices if a['request_id']==receipt['requestID']),None)
        if not advice or receipt['fallback_used'] or advice['model_version']!=m.previous.EXPECTED_MODEL:raise RuntimeError('RF/native correlation missing')
        recommended=[a['gpu_uuid'] for a in advice['assignments']]
        if recommended!=ids:raise RuntimeError('RF/native mismatch')
        proof=dict(case=case,pod_uid=uid,pod_name=pod['metadata']['name'],task_type=pod['metadata']['labels']['gpu-lab/workload-type'],node=pod['spec']['nodeName'],
            rf_uuids=recommended,allocate_uuids=ids,plugin_uuids=consumed,native_receipt=receipt,rf_advice=advice,all_node_advices=advices,
            node_constraint='all_three_nodes' if case in ['training','inference'] else 'explicit_node_path_validation',phase=pod['status']['phase'],cuda_executed=False)
        save(self.out/(case+'-uuid-proof.json'),proof);return proof
    def submit_case(self,case):
        before=self.snapshot(case+'-before')
        if before['node_lock']:raise RuntimeError('NodeLock present')
        docs=list(yaml.safe_load_all((ROOT/(case+'.yaml')).read_text()));self.state['planned']=docs;self.save()
        for o in docs:self.create(o)
        def running():
            self.guard();p=self.get('pod',docs[1]['metadata']['name'],'gpu-rf-tests');save(self.out/(case+'-pod.json'),p)
            if p['status'].get('phase') in ('Failed','Succeeded'):raise RuntimeError('Unexpected terminal sleep')
            return p if p['status'].get('phase')=='Running' else False
        pod=self.wait(running,100);proof=self.evidence(pod,case)
        self.wait(lambda:not any(self.get('node',n)['metadata'].get('annotations',{}).get('hamivgpu') for n in NODES),30)
        after=self.snapshot(case+'-after');old={g['uuid']:g for g in before['devices']};target=proof['allocate_uuids'][0]
        for g in after['devices']:
            delta=tuple(g[k]-old[g['uuid']][k] for k in ['allocated_memory_mib','allocated_compute_quota','allocated_vgpu_count'])
            if delta!=((4096,20,1) if g['uuid']==target else (0,0,0)):raise RuntimeError('Ledger delta mismatch')
        for phase,snap in [('logicalBefore',before),('logicalAfter',after)]:
            direct={g['uuid']:g for g in proof['native_receipt'][phase]}
            for g in snap['devices']:
                if g['node']==proof['node']:
                    for k in ['allocated_memory_mib','allocated_compute_quota','allocated_vgpu_count']:
                        if direct[g['uuid']][k]!=g[k]:raise RuntimeError('Native/API mismatch')
        self.state.setdefault('proofs',[]).append(proof);self.save();self.delete_pod(pod)
        if self.snapshot(case+'-released')['devices']!=before['devices']:raise RuntimeError('Release failed')
        print(case,'VERIFIED',proof['node'],target,flush=True)
    def delete_pod(self,pod):
        # Parent checks only t3dgq NodeLock; additionally check all tested nodes.
        super().delete_pod(pod)
        self.wait(lambda:not any(self.get('node',n)['metadata'].get('annotations',{}).get('hamivgpu') for n in NODES),30)
    def recover(self):
        if self.state_path.exists():self.state=json.loads(self.state_path.read_text())
        if self.state.get('restored'):return
        # Keep RF alive until own Pods are removed and ledgers reconciled.
        self.state['recovering']=True;self.save()
        # Parent adopts uncertain creations and does safe cleanup before final restore.
        self.clean_pods()
        snap=self.snapshot('pre-restore')
        if snap['node_lock'] or any(g['allocated_vgpu_count'] for g in snap['devices']):raise RuntimeError('Cannot safely restore: ledger not empty')
        self.scale_rf(0)
        original=self.prior('deployment-rf');current=self.get('deployment','volcano-rf-test','gpu-rf-system')
        expected=self.state.get('expanded_rf_spec')
        if current['metadata']['uid']!=original['metadata']['uid'] or current['spec'] not in [original['spec'],expected]:raise RuntimeError('Unexpected RF spec')
        if current['spec']!=original['spec']:
            self.cmd(K+['patch','deployment','volcano-rf-test','-n','gpu-rf-system','--type=json','-p',json.dumps([{'op':'test','path':'/metadata/resourceVersion','value':current['metadata']['resourceVersion']},{'op':'replace','path':'/spec','value':original['spec']}])])
        role=self.get('clusterrole','rf-v7-scheduler');prior=self.prior('rbac-original')
        if role['metadata']['uid']!=prior['metadata']['uid'] or role['rules'] not in [prior['rules'],self.state.get('expanded_rules')]:raise RuntimeError('Unexpected RBAC changes')
        if role['rules']!=prior['rules']:
            self.cmd(K+['patch','clusterrole','rf-v7-scheduler','--type=json','-p',json.dumps([{'op':'test','path':'/metadata/resourceVersion','value':role['metadata']['resourceVersion']},{'op':'replace','path':'/rules','value':prior['rules']}])])
        super().recover()
        if self.get('clusterrole','rf-v7-scheduler')['rules']!=prior['rules']:raise RuntimeError('RBAC restoration failed')
        save(self.out/'cross-restoration.json',dict(rbac_restored=True,rf_node_selectors_restored=True,original_scheduler_restored=True))
    def execute(self):
        if not self.prior('cross-prepared')['success'] or not self.prior('authorization')['approved']:raise RuntimeError('Not prepared')
        if self.state.get('old_stopped') or self.state.get('restored'):raise RuntimeError('Do not rerun')
        self.guard();self.state['started_epoch']=time.time();self.save()
        with (self.out/'watchdog.log').open('a') as log:
            p=subprocess.Popen([sys.executable,__file__,'--watchdog',str(os.getpid())],stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
        self.state['watchdog_pid']=p.pid;self.save()
        try:
            old=self.prior('deployment-original')
            if self.get('deployment','volcano-scheduler','volcano-system')['spec']!=old['spec']:raise RuntimeError('Original spec changed')
            self.cmd(K+['scale','deployment','volcano-scheduler','-n','volcano-system','--current-replicas=1','--replicas=0']);self.state['old_stopped']=True;self.save()
            self.wait(lambda:not any(p['metadata']['namespace']=='volcano-system' and p['metadata']['name'].startswith('volcano-scheduler-') for p in self.get('pods')['items']),90)
            self.guard();snap=self.snapshot('original-exited')
            if snap['node_lock'] or any(g['allocated_vgpu_count'] for g in snap['devices']):raise RuntimeError('In-flight reservations')
            role=self.get('clusterrole','rf-v7-scheduler');rules=copy.deepcopy(role['rules'])
            for rule in rules:
                if rule.get('resources')==['nodes'] and rule.get('resourceNames')==['t3dgq']:rule['resourceNames']=NODES
            deployment=self.get('deployment','volcano-rf-test','gpu-rf-system');spec_rf=copy.deepcopy(deployment['spec'])
            c=next(c for c in spec_rf['template']['spec']['containers'] if c['name']=='scheduler')
            c['args']=[a for a in c['args'] if not a.startswith('--node-selector=')]+['--node-selector=kubernetes.io/hostname:'+n for n in NODES]
            self.state.update(expanded_rules=rules,expanded_rf_spec=spec_rf);self.save()
            self.cmd(K+['patch','clusterrole','rf-v7-scheduler','--type=json','-p',json.dumps([{'op':'test','path':'/metadata/resourceVersion','value':role['metadata']['resourceVersion']},{'op':'replace','path':'/rules','value':rules}])])
            self.cmd(K+['patch','deployment','volcano-rf-test','-n','gpu-rf-system','--type=json','-p',json.dumps([{'op':'test','path':'/metadata/resourceVersion','value':deployment['metadata']['resourceVersion']},{'op':'replace','path':'/spec','value':spec_rf}])])
            self.scale_rf(1)
            for case,_,_ in CASES:self.submit_case(case)
            self.state.update(experiment_success=True,finished=True);self.save()
        except BaseException as exc:
            self.state.update(experiment_success=False,error=repr(exc));self.save()
            try:self.logs('failure')
            except Exception:pass
            raise
        finally:self.recover()

def main():
    p=argparse.ArgumentParser();p.add_argument('--prepare',action='store_true');p.add_argument('--execute',action='store_true');p.add_argument('--recover',action='store_true');p.add_argument('--watchdog',type=int);a=p.parse_args();ROOT.mkdir(exist_ok=True)
    if a.watchdog:
        start=time.monotonic()
        while time.monotonic()-start<900:
            time.sleep(5);r=Run()
            if r.state.get('restored'):return
            try:os.kill(a.watchdog,0)
            except ProcessLookupError:break
        Run().recover();return
    r=Run()
    if a.prepare:r.prepare()
    elif a.execute:
        def stop(signum,frame):raise RuntimeError('Interrupted')
        signal.signal(signal.SIGTERM,stop);r.execute()
    elif a.recover:r.recover()

if __name__=='__main__':main()
