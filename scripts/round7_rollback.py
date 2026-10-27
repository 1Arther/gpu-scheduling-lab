"""Review-first rollback: exact recorded objects, refuses foreign UID/labels.

Default prints only. --execute ALSO requires GPU_LAB_ROUND7_ROLLBACK_APPROVED=yes
and an actual created-object UID ledger from the future authorized run. Never
deletes Nodes, Namespaces, CRDs, root/default queues, DQN or Device Plugin objects.
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import time

NAMESPACES={'gpu-rf-tests','gpu-rf-system'}
CLUSTER={'clusterrole','clusterrolebinding','queue'}
RESOURCES={'pod':('/api/v1','pods'),'configmap':('/api/v1','configmaps'),'serviceaccount':('/api/v1','serviceaccounts'),
           'resourcequota':('/api/v1','resourcequotas'),'deployment':('/apis/apps/v1','deployments'),
           'podgroup':('/apis/scheduling.volcano.sh/v1beta1','podgroups'),'queue':('/apis/scheduling.volcano.sh/v1beta1','queues'),
           **{k:('/apis/rbac.authorization.k8s.io/v1',v) for k,v in [('role','roles'),('rolebinding','rolebindings'),('clusterrole','clusterroles'),('clusterrolebinding','clusterrolebindings')]}}


def validate(row):
    kind,name,ns,uid=(row.get(k) for k in ('kind','name','namespace','uid'))
    if not uid or not isinstance(uid,str):raise ValueError('actual created-object UID required')
    if not isinstance(name,str) or not re.fullmatch('[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?',name):raise ValueError('unsafe resource name')
    if kind not in {'pod','podgroup','deployment','configmap','role','rolebinding','serviceaccount','resourcequota',*CLUSTER}:
        raise ValueError('unsupported rollback kind')
    if kind in CLUSTER:
        if ns or not name.startswith('rf-v7-'):raise ValueError('foreign cluster resource')
    elif ns not in NAMESPACES or not (name.startswith('rf-v7-') or name=='volcano-rf-test'):
        raise ValueError('foreign namespaced resource')
    base,plural=RESOURCES[kind]
    endpoint=base+('/namespaces/'+ns if ns else '')+'/'+plural+'/'+name
    return ['kubectl','delete','--raw='+endpoint,'-f','-']


def delete_options(row):
    validate(row)
    return {'apiVersion':'v1','kind':'DeleteOptions','preconditions':{'uid':row['uid']},'propagationPolicy':'Foreground'}


def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--ledger',type=Path,required=True);ap.add_argument('--execute',action='store_true');a=ap.parse_args()
    records=json.loads(a.ledger.read_text(encoding='utf-8'))
    commands=[validate(row) for row in records]
    if a.execute and os.environ.get('GPU_LAB_ROUND7_ROLLBACK_APPROVED')!='yes':raise SystemExit('Explicit rollback approval required')
    if not a.execute:
        print(json.dumps({'dry_run':True,'operations':[{'command':c,'stdin':delete_options(r)} for r,c in zip(records,commands)],'note':'Stop own scheduler only after all own GPU Pods release; never delete namespace or clear hamivgpu lock automatically.'},indent=2));return
    # Validate every object BEFORE deleting any. Record ledger in reverse teardown order.
    for row,command in zip(records,commands):
        r=subprocess.run(['kubectl','get',command[2]],capture_output=True,text=True,check=True)
        obj=json.loads(r.stdout)
        if obj['metadata']['uid']!=row['uid'] or obj['metadata'].get('labels',{}).get('gpu-lab/experiment')!='round7-rf':
            raise SystemExit('Foreign/replaced object; stop rollback')
    if any(row['kind']=='pod' for row in records) and any(row['kind']=='deployment' for row in records):
        raise SystemExit('Rollback GPU Pods first, capture release evidence, then use a separate control-plane ledger')
    for row,command in zip(records,commands):
        # Server-enforced UID precondition prevents deleting a replacement object.
        subprocess.run(command,input=json.dumps(delete_options(row)),text=True,check=True)
        deadline=time.monotonic()+60
        while True:
            r=subprocess.run(['kubectl','get',command[2]],capture_output=True,text=True)
            if r.returncode and 'NotFound' in r.stderr:break
            if r.returncode:raise SystemExit('Cannot confirm deletion: '+r.stderr)
            if json.loads(r.stdout)['metadata']['uid']!=row['uid']:raise SystemExit('Name reused; stop without touching replacement')
            if time.monotonic()>deadline:raise SystemExit('Timed out waiting; inspect resources, do not force delete')
            time.sleep(.5)


if __name__=='__main__':main()
