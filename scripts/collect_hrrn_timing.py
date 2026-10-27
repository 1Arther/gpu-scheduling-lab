"""Offline analysis of exported Kubernetes Pods and timestamped scheduler logs.

No cluster client. Missing measurements remain null, never estimated from sleep.
"""
import argparse
import csv
import json
import re
from datetime import datetime
from pathlib import Path

def timestamp(value):
    if not value:return None
    return datetime.fromisoformat(value.replace('Z','+00:00')).timestamp()

def difference(end,start):
    return None if end is None or start is None else end-start

def extract(pods,log,decisions=None,external_ready=None):
    ready, released={},{}
    for line in log.splitlines():
        # kubectl logs --timestamps adds an unambiguous RFC3339 prefix.
        if 'HRRN session snapshot' in line and 'ready=true' in line:
            job=re.search(r'\bjob="([^"]+)"',line)
            at=re.search(r'\breadyAt="([^"]+)"',line)
            if job and at:ready[job[1]]=min(ready.get(job[1],float('inf')),timestamp(at[1]))
        if 'rf-job logical release' in line:
            pod=re.search(r'\bpod="([^"]+)"',line)
            try:at=timestamp(line.split()[0])
            except (ValueError,IndexError):continue
            if pod:released[pod[1]]=max(released.get(pod[1],0),at)
    if external_ready is not None:ready=external_ready
    rows=[]
    for p in pods['items']:
        md,sp,st=p['metadata'],p['spec'],p.get('status',{})
        key=md['namespace']+'/'+md['name']
        group=md.get('annotations',{}).get('scheduling.k8s.io/group-name')
        jobkey=md['namespace']+'/'+group if group else None
        scheduled=next((timestamp(c['lastTransitionTime']) for c in st.get('conditions',[]) if c['type']=='PodScheduled' and c['status']=='True'),None)
        starts,finishes=[],[]
        for c in st.get('containerStatuses',[]):
            state=c.get('state',{})
            if 'terminated' in state:
                starts.append(timestamp(state['terminated'].get('startedAt')))
                finishes.append(timestamp(state['terminated'].get('finishedAt')))
            elif 'running' in state:starts.append(timestamp(state['running'].get('startedAt')))
        starts=[x for x in starts if x is not None];finishes=[x for x in finishes if x is not None]
        start=min(starts) if starts else None
        finish=max(finishes) if len(finishes)==len(sp.get('containers',[])) and finishes else None
        begin=ready.get(jobkey)
        rows.append(dict(pod=key,uid=md.get('uid'),job=jobkey,created_at=timestamp(md.get('creationTimestamp')),
            ready_at=begin,ready_source='common_external_observation' if external_ready is not None else 'hrrn_session_log',scheduled_at=scheduled,container_started_at=start,completed_at=finish,
            logical_release_at=released.get(key),queue_wait_seconds=difference(scheduled,begin),
            scheduling_decision_seconds=(decisions or {}).get(key),
            pod_start_delay_seconds=difference(start,scheduled),container_runtime_seconds=difference(finish,start),
            release_delay_seconds=difference(released.get(key),finish),phase=st.get('phase'),
            gpu_uuids=md.get('annotations',{}).get('volcano.sh/vgpu-ids-new'),
            evidence='exported cluster observations; sleep only, not a GPU performance model'))
    return rows

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--pods',type=Path,required=True);p.add_argument('--log',type=Path,required=True)
    p.add_argument('--decisions',type=Path,help='Optional independently instrumented CSV: pod,scheduling_decision_seconds')
    p.add_argument('--readiness',type=Path,help='Common external observation CSV for ALL modes: job,ready_at (RFC3339)')
    p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    decisions={}
    if a.decisions:
        with a.decisions.open(encoding='utf-8-sig') as f:decisions={r['pod']:float(r['scheduling_decision_seconds']) for r in csv.DictReader(f)}
    ready=None
    if a.readiness:
        with a.readiness.open(encoding='utf-8-sig') as f:ready={r['job']:timestamp(r['ready_at']) for r in csv.DictReader(f)}
    rows=extract(json.loads(a.pods.read_text(encoding='utf-8-sig')),a.log.read_text(encoding='utf-8-sig'),decisions,ready)
    a.output.parent.mkdir(parents=True,exist_ok=True)
    with a.output.open('w',newline='',encoding='utf-8') as f:
        if rows:
            w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
    print(json.dumps({'pods':len(rows),'missing_ready':sum(r['ready_at'] is None for r in rows),'missing_decision_measurement':sum(r['scheduling_decision_seconds'] is None for r in rows)}))
