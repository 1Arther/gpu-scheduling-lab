"""Read-only capture for AFTER authorized deployment; creates local evidence only.

Uses GET/watch-like polling and logs. Does not create Pods, port-forward, exec or
modify configuration. Node checkpoint capture uses round7_collect.py separately.
"""
import argparse
import datetime as dt
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from gpu_lab.staging import allocations


def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--output',type=Path,required=True)
    ap.add_argument('--seconds',type=int,default=120);ap.add_argument('--interval',type=float,default=1)
    ap.add_argument('--kubeconfig');a=ap.parse_args()
    if not 1<=a.seconds<=900 or a.interval<.5:raise SystemExit('Use 1..900 seconds and interval >= .5')
    out=a.output;out.mkdir(parents=True,exist_ok=False)
    prefix=['kubectl']+(['--kubeconfig='+a.kubeconfig] if a.kubeconfig else [])
    end=time.monotonic()+a.seconds
    with (out/'pod-events.jsonl').open('w',encoding='utf-8') as f:
        seen=set()
        while time.monotonic()<end:
            command=prefix+['get','pods','-n','gpu-rf-tests','-l','gpu-lab/experiment=round7-rf','-o','json','--request-timeout=8s']
            r=subprocess.run(command,capture_output=True,text=True)
            if r.returncode:
                f.write(json.dumps({'utc':dt.datetime.now(dt.timezone.utc).isoformat(),'command':command,'error':r.stderr})+'\n');f.flush()
            else:
                for p in json.loads(r.stdout).get('items',[]):
                    m=p['metadata'];key=(m['uid'],m['resourceVersion'])
                    if key in seen:continue
                    seen.add(key);ann=m.get('annotations',{})
                    data={'utc':dt.datetime.now(dt.timezone.utc).isoformat(),'metadata':{k:m.get(k) for k in ('name','namespace','uid','resourceVersion','labels','deletionTimestamp')},
                          'spec':{k:p['spec'].get(k) for k in ('nodeName','schedulerName')},'status':p.get('status'),
                          'requests':[{'name':c['name'],'resources':c.get('resources',{})} for c in p['spec']['containers']],
                          'annotations':{k:v for k,v in ann.items() if k.startswith('volcano.sh/')},
                          'persistent_allocations':allocations(ann.get('volcano.sh/vgpu-ids-new')),
                          'transient_allocations':allocations(ann.get('volcano.sh/devices-to-allocate'))}
                    f.write(json.dumps(data,ensure_ascii=False)+'\n');f.flush()
            time.sleep(min(a.interval,max(0,end-time.monotonic())))
    for c in ('rf-model','scheduler'):
        command=prefix+['logs','-n','gpu-rf-system','deployment/volcano-rf-test','-c',c,'--timestamps','--request-timeout=10s']
        r=subprocess.run(command,capture_output=True,text=True)
        (out/(c+'.log')).write_text(r.stdout,encoding='utf-8')
        (out/(c+'-transport.json')).write_text(json.dumps({'command':command,'exit_code':r.returncode,'stderr':r.stderr}),encoding='utf-8')
    print(json.dumps({'output':str(out),'cluster_resources_changed':False,'plugin_consumed_uuid_evidence':'collect checkpoint on the allocated node; do not infer from Running'}))


if __name__=='__main__':main()
