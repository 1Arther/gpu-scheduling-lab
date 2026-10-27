"""Remove only this experiment's admission guard, LAST in approved rollback.

Default prints the UID-conditional deletion plan. No cluster writes unless
--execute AND GPU_LAB_ROUND7_ROLLBACK_APPROVED=yes. Keep the guard while any
experiment Pod or RF scheduler process can still reserve quota.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess

NAME='rf-v7-gpu-window'
KINDS={'ValidatingAdmissionPolicyBinding':'validatingadmissionpolicybindings',
       'ValidatingAdmissionPolicy':'validatingadmissionpolicies'}


def operation(row):
    if row.get('kind') not in KINDS or row.get('name')!=NAME or row.get('namespace') or not row.get('uid'):
        raise ValueError('Only recorded RF window policy/binding UIDs allowed')
    endpoint='/apis/admissionregistration.k8s.io/v1/'+KINDS[row['kind']]+'/'+NAME
    return ['kubectl','delete','--raw='+endpoint,'-f','-'],{'apiVersion':'v1','kind':'DeleteOptions','preconditions':{'uid':row['uid']}}


def may_remove_guard(pods,deployment,scheduler_pods):
    if pods:return False
    if deployment and (deployment.get('spec',{}).get('replicas',1) or deployment.get('status',{}).get('replicas',0)):
        return False
    return not any(p.get('status',{}).get('phase') not in ['Succeeded','Failed'] for p in scheduler_pods)


def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--ledger',type=Path,required=True);ap.add_argument('--execute',action='store_true');a=ap.parse_args()
    rows=json.loads(a.ledger.read_text());rows.sort(key=lambda r:r.get('kind')!='ValidatingAdmissionPolicyBinding')
    ops=[operation(r) for r in rows]
    if not a.execute:
        print(json.dumps({'dry_run':True,'operations':[{'command':c,'stdin':body} for c,body in ops],
          'required_order':'Delete own GPU Pods, prove release, stop RF scheduler, remove own control plane, THEN remove window binding/policy. Original DQN is untouched.'},indent=2));return
    if os.environ.get('GPU_LAB_ROUND7_ROLLBACK_APPROVED')!='yes':raise SystemExit('Explicit rollback approval required')
    def get(args):
        cmd=['kubectl','get',*args]
        if not (len(args)==1 and args[0].startswith('--raw=')):cmd+=['--ignore-not-found','-o','json']
        r=subprocess.run(cmd,capture_output=True,text=True)
        if r.returncode and 'NotFound' in r.stderr:return None
        r.check_returncode()
        return json.loads(r.stdout) if r.stdout.strip() else None
    pods=(get(['pods','-n','gpu-rf-tests','-l','gpu-lab/experiment=round7-rf']) or {}).get('items',[])
    dep=get(['deployment','volcano-rf-test','-n','gpu-rf-system'])
    scheduler=(get(['pods','-n','gpu-rf-system','-l','app=volcano-rf-test']) or {}).get('items',[])
    if not may_remove_guard(pods,dep,scheduler):raise SystemExit('Keep guard: experiment Pods or RF scheduler still present')
    for row,(command,_) in zip(rows,ops):
        obj=get([command[2]])
        if not obj or obj['metadata']['uid']!=row['uid'] or obj['metadata'].get('labels',{}).get('gpu-lab/experiment')!='round7-rf':
            raise SystemExit('Missing/replaced/foreign guard: stop without deletion')
    for row,(command,body) in zip(rows,ops):
        subprocess.run(command,input=json.dumps(body),text=True,check=True)
        obj=get([command[2]])
        if obj:raise SystemExit('Guard name remains/replaced: inspect, do not force delete')


if __name__=='__main__':main()
