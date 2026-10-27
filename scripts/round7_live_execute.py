"""Authorized, bounded real-cluster sleep validation; restores the original replicas.

No CUDA workloads, device resets, checkpoint/NodeLock cleanup or DQN config edits.
Run only in the independent 4090 experiment directory with the approved token.
"""
import argparse
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.request import urlopen

import yaml

ROOT=Path(os.environ.get('GPU_LAB_ROUND7_RUN_ROOT','/home/zbs/rf-round7-live-window-20261019'))
LAB=ROOT/'lab'
K=['sudo','-n','kubectl','--kubeconfig=/etc/kubernetes/admin.conf']
EXPECTED_MODEL='rf-v4-known-states-ee35cfabe30f'
LABEL='gpu-lab/experiment=round7-rf'
sys.path.insert(0,str(LAB));sys.path.insert(0,str(LAB/'scripts'))
from gpu_lab.staging import allocations, inventory, native_commit_receipt
from round7_collect import checkpoint
from round7_window_preflight import gpu_demand, blockers


def now():return dt.datetime.now(dt.timezone.utc).isoformat()


class Run:
    def __init__(self):
        self.out=ROOT/'execution';self.out.mkdir(exist_ok=True)
        self.state_path=self.out/'state.json'
        self.state=json.loads(self.state_path.read_text()) if self.state_path.exists() else dict(created=[],pods=[],old_stopped=False,rf_started=False,finished=False)
        self.counter=len(list(self.out.glob('command-*.json')))

    def event(self,event,**data):
        with (self.out/'events.jsonl').open('a') as f:f.write(json.dumps(dict(utc=now(),event=event,**data))+'\n')
        (self.out/'heartbeat').touch()

    def save(self):
        p=self.state_path.with_suffix('.tmp');p.write_text(json.dumps(self.state,indent=2));p.replace(self.state_path)

    def cmd(self,args,body=None,check=True,timeout=35):
        self.event('command',args=args)
        p=subprocess.run(args,input=json.dumps(body) if body is not None else None,capture_output=True,text=True,timeout=timeout)
        self.counter+=1
        record=dict(utc=now(),command=args,stdin=body,exit_code=p.returncode,stdout=p.stdout,stderr=p.stderr)
        (self.out/f'command-{self.counter:05}.json').write_text(json.dumps(record,indent=2))
        if check and p.returncode:raise RuntimeError('Command failed: '+' '.join(args)+' '+p.stderr[-1500:])
        return p

    def get(self,kind,name=None,ns=None,optional=False):
        args=K+['get',kind]+([name] if name else [])+(['-n',ns] if ns else (['-A'] if kind=='pods' else []))
        if optional:args+=['--ignore-not-found']
        p=self.cmd(args+['-o','json','--request-timeout=15s'])
        return json.loads(p.stdout) if p.stdout.strip() else None

    def wait(self,condition,seconds=100):
        end=time.monotonic()+seconds
        while True:
            value=condition()
            if value:return value
            if time.monotonic()>end:raise RuntimeError('Timed out waiting for '+condition.__name__)
            time.sleep(1)

    def guard(self):
        if (ROOT/'STOP').exists():raise RuntimeError('Operator STOP requested')
        pods=self.get('pods')['items']
        own={r['uid'] for r in self.state['pods']}
        bad=[p['metadata']['namespace']+'/'+p['metadata']['name'] for p in pods
             if p.get('status',{}).get('phase') not in ['Succeeded','Failed'] and gpu_demand(p) and p['metadata']['uid'] not in own]
        if bad:raise RuntimeError('Foreign live GPU/DRA requests appeared: '+str(bad))
        if self.state['old_stopped']:
            old=self.get('deployment','volcano-scheduler','volcano-system')
            if old['spec']['replicas']!=0:raise RuntimeError('Original scheduler resumed unexpectedly')
            if any(p['metadata']['name'].startswith('volcano-scheduler-') and p['metadata']['namespace']=='volcano-system' for p in pods):
                raise RuntimeError('Original scheduler Pod still exists')
        return pods

    def snapshot(self,name):
        pods=self.guard();node=self.get('node','t3dgq')
        devices,warnings=inventory([node],pods)
        if warnings:raise RuntimeError('Unreconciled logical annotations: '+str(warnings))
        if len(devices)!=4:raise RuntimeError('Expected exactly four registered 4090 GPUs')
        for g in devices:
            if min(g['free_memory_mib'],g['free_compute_quota'],g['free_instances'])<0:raise RuntimeError('Logical over-allocation')
        result=dict(utc=now(),source='API annotation reconstruction; separate from direct RF/native logs',devices=devices,node_lock=node['metadata'].get('annotations',{}).get('hamivgpu'))
        (self.out/(name+'-ledger.json')).write_text(json.dumps(result,indent=2));return result

    def create(self,obj):
        p=self.cmd(K+['create','-f','-','-o','json','--request-timeout=20s'],obj)
        result=json.loads(p.stdout);m=result['metadata']
        row=dict(kind=result['kind'],name=m['name'],namespace=m.get('namespace'),uid=m['uid'])
        self.state['created'].append(row)
        if obj['kind']=='Pod':self.state['pods'].append(row)
        self.save();self.event('created',**row);return result

    def scheduler_pods(self):
        return [p for p in self.get('pods')['items'] if p['metadata']['namespace']=='gpu-rf-system' and p['metadata'].get('labels',{}).get('app')=='volcano-rf-test']

    def scale_rf(self,n):
        self.cmd(K+['-n','gpu-rf-system','scale','deployment','volcano-rf-test','--replicas='+str(n),'--request-timeout=20s'])
        self.state['rf_started']=bool(n);self.save()
        if n:
            self.cmd(K+['-n','gpu-rf-system','rollout','status','deployment/volcano-rf-test','--timeout=120s'],timeout=130)
            self.wait(lambda:any(p['status'].get('phase')=='Running' and all(c.get('ready') for c in p['status'].get('containerStatuses',[])) for p in self.scheduler_pods()),30)
        else:self.wait(lambda:not self.scheduler_pods(),100)

    def logs(self,name):
        for container in ['scheduler','rf-model']:
            p=self.cmd(K+['logs','-n','gpu-rf-system','deployment/volcano-rf-test','-c',container,'--timestamps','--request-timeout=15s'],check=False)
            (self.out/(name+'-'+container+'.log')).write_text(p.stdout)
        pods=self.scheduler_pods()
        if pods and pods[0]['status'].get('podIP'):
            try:
                text=urlopen('http://'+pods[0]['status']['podIP']+':18090/metrics',timeout=3).read().decode()
                (self.out/(name+'-metrics.txt')).write_text(text)
            except Exception as exc:self.event('metrics_unavailable',error=str(exc))

    def evidence(self,pod,name,require_rf=True):
        self.logs(name);uid=pod['metadata']['uid']
        native=allocations(pod['metadata'].get('annotations',{}).get('volcano.sh/vgpu-ids-new'))
        if len(native)!=1:raise RuntimeError('Not exactly one native physical UUID')
        raw=json.loads(self.cmd(['sudo','-n','cat','/var/lib/kubelet/device-plugins/kubelet_internal_checkpoint']).stdout)
        cp=checkpoint(raw)
        (self.out/(name+'-checkpoint.json')).write_text(json.dumps(cp,indent=2))
        matching=[e for e in cp['entries'] if e['PodUID']==uid and e['ContainerName']=='sleep' and e['ResourceName']=='volcano.sh/vgpu-number']
        if len(matching)!=1:raise RuntimeError('Missing matching kubelet AllocateResponse')
        consumed=matching[0].get('allocate_envs',{}).get('NVIDIA_VISIBLE_DEVICES','').split(',')
        expected=[d['uuid'] for d in native]
        if consumed!=expected:raise RuntimeError('Native/plugin UUID mismatch')
        scheduler=(self.out/(name+'-scheduler.log')).read_text()
        commit=[s for s in scheduler.splitlines() if 'rf-job UUID allocation' in s and uid in s]
        if len(commit)!=1:raise RuntimeError('Expected one native commit log for '+uid)
        request=re.search(r'requestID="([^"]*)"',commit[0])
        rf=[]
        for line in (self.out/(name+'-rf-model.log')).read_text().splitlines():
            try:o=json.loads(line[line.index('{'):])
            except (ValueError,json.JSONDecodeError):continue
            if o.get('event')=='rf_advice' and o.get('pod',{}).get('uid')==uid:rf.append(o)
        advice=next((o for o in rf if request and o.get('request_id')==request.group(1)),None)
        if require_rf:
            if not advice:raise RuntimeError('No RF advice correlated to native requestID')
            recommended=[a['gpu_uuid'] for a in advice['assignments']]
            if recommended!=expected:raise RuntimeError('RF/native UUID mismatch')
            if advice['model_version']!=EXPECTED_MODEL:raise RuntimeError('Model version mismatch')
        else:
            recommended=None
            if not native_commit_receipt(commit[0])['fallback_used']:raise RuntimeError('Native fallback not recorded')
        result=dict(utc=now(),pod_uid=uid,pod_name=pod['metadata']['name'],workload_type=pod['metadata']['labels']['gpu-lab/workload-type'],
            rf_recommended_uuids=recommended,native_permanent_uuids=expected,device_plugin_consumed_uuids=consumed,
            device_plugin_evidence_source='matching Pod UID/container/vgpu-number kubelet AllocateResponse.NVIDIA_VISIBLE_DEVICES',
            native_commit_log=commit[0],rf_advice=advice,real_pod_phase=pod['status']['phase'],cuda_executed=False)
        (self.out/(name+'-uuid-proof.json')).write_text(json.dumps(result,indent=2));self.event('uuid_verified',uid=uid,uuids=expected,rf_correlated=bool(advice));return result

    def submit(self,file,name,require_rf=True):
        before=self.snapshot(name+'-before')
        if before['node_lock']:raise RuntimeError('NodeLock still present before next Pod')
        docs=list(yaml.safe_load_all((LAB/'k8s/round7-logical-window'/file).read_text()))
        for obj in docs:self.create(obj)
        pod_name=next(o['metadata']['name'] for o in docs if o['kind']=='Pod')
        def running():
            self.guard();p=self.get('pod',pod_name,'gpu-rf-tests')
            (self.out/(name+'-pod.json')).write_text(json.dumps(p,indent=2))
            if p['status'].get('phase') in ['Failed','Succeeded']:raise RuntimeError('Unexpected terminal sleep Pod')
            return p if p['status'].get('phase')=='Running' else False
        pod=self.wait(running,120)
        self.wait(lambda:not self.get('node','t3dgq')['metadata'].get('annotations',{}).get('hamivgpu'),30)
        proof=self.evidence(pod,name,require_rf);after=self.snapshot(name+'-after')
        target=proof['native_permanent_uuids'][0]
        old={d['uuid']:d for d in before['devices']}
        for d in after['devices']:
            delta=(d['allocated_memory_mib']-old[d['uuid']]['allocated_memory_mib'],d['allocated_compute_quota']-old[d['uuid']]['allocated_compute_quota'],d['allocated_vgpu_count']-old[d['uuid']]['allocated_vgpu_count'])
            if delta!=((4096,20,1) if d['uuid']==target else (0,0,0)):raise RuntimeError('Wrong logical allocation delta: '+str(delta))
        if proof['rf_advice']:
            inputs={g['uuid']:g for g in proof['rf_advice']['gpus']}
            for uuid,d in old.items():
                for key in ['allocated_memory_mib','allocated_compute_quota','allocated_vgpu_count']:
                    if inputs[uuid][key]!=d[key]:raise RuntimeError('RF did not read latest logical state')
        return pod,proof

    def delete_pod(self,pod):
        name=pod['metadata']['name'];uid=pod['metadata']['uid']
        current=self.get('pod',name,'gpu-rf-tests',optional=True)
        if not current:return
        if current['metadata']['uid']!=uid or current['metadata'].get('labels',{}).get('gpu-lab/experiment')!='round7-rf':raise RuntimeError('Refuse foreign/replaced Pod')
        self.cmd(K+['delete','--raw=/api/v1/namespaces/gpu-rf-tests/pods/'+name,'-f','-'],
            dict(apiVersion='v1',kind='DeleteOptions',preconditions=dict(uid=uid),gracePeriodSeconds=5))
        self.wait(lambda:not self.get('pod',name,'gpu-rf-tests',optional=True),60)
        self.wait(lambda:not self.get('node','t3dgq')['metadata'].get('annotations',{}).get('hamivgpu'),30)
        def released():
            self.logs('released-'+uid)
            text=(self.out/('released-'+uid+'-scheduler.log')).read_text()
            return any('rf-job logical release' in line and uid in line for line in text.splitlines())
        self.wait(released,20)
        self.event('pod_released',uid=uid,name=name)

    def clean_pods(self):
        byuid={r['uid'] for r in self.state['pods']}
        for p in self.get('pods')['items']:
            if p['metadata']['uid'] in byuid:self.delete_pod(p)

    def switch(self,config):
        self.clean_pods();snap=self.snapshot('baseline-'+config)
        if any(d['allocated_vgpu_count'] for d in snap['devices']):raise RuntimeError('Baseline did not restore')
        self.scale_rf(0)
        self.cmd(K+['patch','deployment','volcano-rf-test','-n','gpu-rf-system','--type=strategic','-p',json.dumps({'spec':{'template':{'spec':{'volumes':[{'name':'config','configMap':{'name':config}}]}}}}),'--request-timeout=20s'])
        self.scale_rf(1)

    def recover(self):
        with (self.out/'restore.lock').open('w') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            # Reload progress after taking the lock (a watchdog may have restored).
            if self.state_path.exists():self.state=json.loads(self.state_path.read_text())
            if self.state.get('restored'):return
            errors=[]
            try:self.clean_pods()
            except Exception as exc:errors.append('pod_cleanup: '+str(exc))
            try:
                if self.get('deployment','volcano-rf-test','gpu-rf-system',optional=True):self.scale_rf(0)
            except Exception as exc:errors.append('rf_stop: '+str(exc))
            original=json.loads((ROOT/'deployment-original.json').read_text())
            current=self.get('deployment','volcano-scheduler','volcano-system')
            if current['metadata']['uid']!=original['metadata']['uid']:raise RuntimeError('Original scheduler UID changed; refuse restore overwrite')
            if self.scheduler_pods():raise RuntimeError('RF Pods remain; do not restart competing scheduler')
            self.cmd(K+['-n','volcano-system','scale','deployment','volcano-scheduler','--replicas='+str(original['spec']['replicas']),'--request-timeout=20s'])
            self.cmd(K+['-n','volcano-system','rollout','status','deployment/volcano-scheduler','--timeout=120s'],timeout=130)
            final=self.get('deployment','volcano-scheduler','volcano-system')
            cm=self.get('configmap','volcano-scheduler-configmap','volcano-system')
            oldcm=json.loads((ROOT/'configmap-original.json').read_text())
            if final['spec']!=original['spec'] or cm['data']!=oldcm['data']:raise RuntimeError('Original scheduler spec/config not restored exactly')
            self.state.update(restored=True,restored_utc=now(),cleanup_errors=errors);self.save();self.event('original_scheduler_restored',replicas=final['spec']['replicas'],errors=errors)
            (self.out/'restore-confirmation.json').write_text(json.dumps(dict(utc=now(),replicas=final['spec']['replicas'],available=final['status'].get('availableReplicas'),
                original_spec_equal=True,original_config_data_equal=True,original_config_uid=cm['metadata']['uid'],config_resource_version=cm['metadata']['resourceVersion'],rf_pods_remaining=0,errors=errors),indent=2))

    def execute(self):
        if os.environ.get('GPU_LAB_APPROVED_LIVE_WINDOW')!='20261019':raise RuntimeError('Explicit authorized window token required')
        self.guard();node=self.get('node','t3dgq');b=blockers(self.get('pods')['items'],'t3dgq')
        if b['active_gpu_or_DRA_demand'] or b['finite_plugin_candidates'] or node['metadata'].get('annotations',{}).get('hamivgpu'):raise RuntimeError('Existing requests or pending plugin allocation')
        build=json.loads((ROOT/'build/record.json').read_text());assert build['success']
        from round7_render_minimal import render
        objects=render(build['images']['scheduler']['immutable_reference'],build['images']['rf']['immutable_reference'])
        (self.out/'control-plane.yaml').write_text(yaml.safe_dump_all(objects,sort_keys=False))
        original=json.loads((ROOT/'deployment-original.json').read_text())
        if self.get('deployment','volcano-scheduler','volcano-system')['spec']!=original['spec']:raise RuntimeError('Original deployment changed after backup')
        # Recovery process is independent of this SSH session and reads the durable journal.
        watchdog_log=(self.out/'watchdog.log').open('a')
        proc=subprocess.Popen([sys.executable,__file__,'--watchdog'],stdin=subprocess.DEVNULL,stdout=watchdog_log,stderr=subprocess.STDOUT,start_new_session=True)
        self.state['watchdog_pid']=proc.pid;self.save()
        try:
            self.cmd(K+['-n','volcano-system','scale','deployment','volcano-scheduler','--current-replicas='+str(original['spec']['replicas']),'--replicas=0','--request-timeout=20s'])
            self.state.update(old_stopped=True,stopped_utc=now());self.save()
            self.wait(lambda:not any(p['metadata']['namespace']=='volcano-system' and p['metadata']['name'].startswith('volcano-scheduler-') for p in self.get('pods')['items']),100)
            cri=json.loads(self.cmd(['sudo','-n','crictl','ps','-o','json']).stdout)
            if any(c.get('metadata',{}).get('name')=='volcano-scheduler' for c in cri.get('containers',[])):
                raise RuntimeError('Original scheduler container is still running')
            self.snapshot('after-original-stopped')
            for obj in objects:self.create(obj)
            self.scale_rf(1);self.logs('rf-startup');self.snapshot('rf-initial')
            a,pa=self.submit('test-a-training.yaml','a-training')
            b,pb=self.submit('test-b-inference.yaml','b-inference')
            self.delete_pod(a);self.snapshot('after-a-release')
            c,pc=self.submit('test-d-after-release.yaml','c-after-release')
            self.clean_pods();self.snapshot('after-dynamic-cleanup')
            on,pon=self.submit('test-e-control.yaml','control-rf-on');self.delete_pod(on)
            # Reuse the same PodGroup for the same Pod name in the paired control.
            pg=self.get('podgroup','rf-v7-e-control','gpu-rf-tests',optional=True)
            if pg:self.cmd(K+['delete','--raw=/apis/scheduling.volcano.sh/v1beta1/namespaces/gpu-rf-tests/podgroups/rf-v7-e-control','-f','-'],dict(apiVersion='v1',kind='DeleteOptions',preconditions=dict(uid=pg['metadata']['uid'])))
            self.switch('rf-v7-config-no-prediction')
            off,poff=self.submit('test-e-control.yaml','control-rf-off');self.delete_pod(off)
            self.switch('rf-v7-config-service-unavailable')
            fail,pfail=self.submit('test-f-fallback.yaml','service-fallback',False);self.delete_pod(fail)
            self.snapshot('final-released');self.state.update(experiment_success=True,finished=True);self.save()
        except Exception as exc:
            self.state.update(experiment_success=False,error=repr(exc));self.save();self.event('experiment_error',error=repr(exc))
            try:self.logs('failure')
            except Exception:pass
            raise
        finally:self.recover()


def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--watchdog',action='store_true');ap.add_argument('--recover',action='store_true');a=ap.parse_args()
    if a.watchdog:
        started=time.monotonic()
        while time.monotonic()-started<1200:
            time.sleep(5);r=Run()
            if r.state.get('restored'):return
            hb=r.out/'heartbeat'
            if hb.exists() and time.time()-hb.stat().st_mtime>180:break
        Run().recover();return
    r=Run()
    if a.recover:r.recover()
    else:r.execute()


if __name__=='__main__':main()
