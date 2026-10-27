"""GET-only logical-window preflight. Never opens a window, applies or deletes."""
import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import subprocess

MAX_TIME=2**64-1


def gpu_demand(p):
    s=p.get('spec',{})
    if s.get('resourceClaims'):return True
    for c in s.get('containers',[])+s.get('initContainers',[]):
        for typ in ['limits','requests']:
            if any(k=='nvidia.com/gpu' or k.startswith(('nvidia.com/mig-','volcano.sh/vgpu-')) for k in c.get('resources',{}).get(typ,{})):return True
    a=p.get('metadata',{}).get('annotations',{})
    return any(a.get(k) for k in ['volcano.sh/devices-to-allocate','volcano.sh/vgpu-ids-new','volcano.sh/vgpu-node'])


def blockers(pods,node):
    active=[];consumable=[];historical=[]
    for p in pods:
        m=p.get('metadata',{});a=m.get('annotations',{});phase=p.get('status',{}).get('phase')
        ref={k:m.get(k) for k in ['namespace','name','uid']};ref['phase']=phase
        if phase not in ['Succeeded','Failed'] and gpu_demand(p):active.append(ref)
        if a.get('volcano.sh/vgpu-node')!=node:continue
        try:timestamp=int(a.get('volcano.sh/vgpu-time',MAX_TIME))
        except (ValueError,TypeError):timestamp=-1
        if timestamp<MAX_TIME and (a.get('volcano.sh/devices-to-allocate') or a.get('volcano.sh/bind-phase')=='allocating'):
            consumable.append(ref|{'annotations':{k:v for k,v in a.items() if k.startswith('volcano.sh/')}})
        elif phase in ['Succeeded','Failed']:historical.append(ref|{'vgpu_time':timestamp,'transient_empty':not a.get('volcano.sh/devices-to-allocate')})
    return {'active_gpu_or_DRA_demand':active,'finite_plugin_candidates':consumable,'historical_terminal_annotations':historical}


def assessment(result):
    """Observation only: neither an absent nor a present guard authorizes a run."""
    clear = (not result.get('error') and result.get('no_prior_gpu_demand') is True
             and result.get('no_legacy_consumable_annotations') is True
             and not result.get('node_lock'))
    return dict(observed_api_clear=bool(clear), execution_authorized=False,
        admission_policy_mandatory=False,
        private_scheduler_reservations='not_directly_observed',
        preferred_submission_boundary='administrator_coordinated_window_if_all_relevant_submitters_are_controlled',
        technical_exclusion_without_admission=False,
        decision=('HOLD: API observations clear; need explicit authorization, coordinated submission ownership, '
                  'in-flight reservation reconciliation, fixed images and startup consent'
                  if clear else 'HOLD: resolve query errors, live GPU/DRA requests, unconsumed allocations or NodeLock first'),
        alternative='If coordination cannot exclude competing submissions, separately approve enforced admission or one scheduler authority; do not deploy competing allocators.')


def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--node',default='t3dgq',choices=['t3dgq','t3dmg','t3dgq-2'])
    ap.add_argument('--kubeconfig');ap.add_argument('--sudo-kubectl',action='store_true');ap.add_argument('--output',type=Path)
    a=ap.parse_args();prefix=(['sudo','-n'] if a.sudo_kubectl else [])+['kubectl']+(['--kubeconfig='+a.kubeconfig] if a.kubeconfig else [])
    commands=[]
    def get(kind,name=None,namespace=None,optional=False):
        cmd=prefix+['get',kind]+([name] if name else [])+(['-n',namespace] if namespace else (['-A'] if kind=='pods' else []))+(['--ignore-not-found'] if optional else [])+['-o','json','--request-timeout=12s']
        r=subprocess.run(cmd,capture_output=True,text=True,timeout=20)
        commands.append({'command':cmd,'exit_code':r.returncode,'stderr':r.stderr,'sha256':hashlib.sha256(r.stdout.encode()).hexdigest()})
        if r.returncode:raise RuntimeError(r.stderr)
        return json.loads(r.stdout) if r.stdout.strip() else None
    result={'utc':dt.datetime.now(dt.timezone.utc).isoformat(),'read_only':True,'cluster_resources_changed':False,'node':a.node}
    try:
        pods=get('pods')['items'];node=get('node',a.node)
        policy=get('validatingadmissionpolicy','rf-v7-gpu-window',optional=True)
        binding=get('validatingadmissionpolicybinding','rf-v7-gpu-window',optional=True)
        dep=get('deployment','volcano-rf-test','gpu-rf-system',optional=True)
        status=blockers(pods,a.node)
        result.update(status,registered_devices=node.get('metadata',{}).get('annotations',{}).get('volcano.sh/node-vgpu-register'),
          node_lock=node.get('metadata',{}).get('annotations',{}).get('hamivgpu'),
          ordinary_gpu_capacity=node.get('status',{}).get('capacity',{}).get('nvidia.com/gpu'),
          policy_present=bool(policy),binding_present=bool(binding),rf_deployment_present=bool(dep),
          guard_actions=(binding or {}).get('spec',{}).get('validationActions'),
          guard_typechecking=(policy or {}).get('status'),no_prior_gpu_demand=not status['active_gpu_or_DRA_demand'],
          no_legacy_consumable_annotations=not status['finite_plugin_candidates'])
    except Exception as exc:result['error']=type(exc).__name__+': '+str(exc)
    result.update(assessment(result))
    result.update(commands=commands,
      important='GPU physical use is not a veto here. API data does not directly expose pending scheduler-private reservations. This script never starts a scheduler or approves a deployment.')
    text=json.dumps(result,ensure_ascii=False,indent=2)
    if a.output:a.output.write_text(text,encoding='utf-8')
    else:print(text)


if __name__=='__main__':main()
