"""Prepare isolated single-Pod HRRN A/B artifacts; never contacts a cluster."""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import hrrn_stage5_manifests as old

NODES = {'t3dgq': ('172.16.10.33', 'RTX4090', 4),
         't3dmg': ('172.16.20.34', 'RTX5090', 4),
         't3dgq-2': ('172.16.10.34', 'L40', 3)}

def configurations():
    result = {k: v for k, v in old.configs().items() if k in ('A', 'B')}
    for config in result.values():
        args = config['tiers'][1]['plugins'][-1]['arguments']
        args.update({'complete-job': False, 'coordinated-complete-job': False,
                     'reservation-barrier': False})
    return result

def validate_config(config, mode):
    expected = configurations()[mode]
    if config != expected:
        raise ValueError('Only separately reviewed basic A/B configurations are allowed')
    # New independent configs; never patch flags on a running guarded config.
    hrrn = [p for tier in config['tiers'] for p in tier['plugins'] if p['name'] == 'hrrn']
    if len(hrrn) != 1:
        raise ValueError('Duplicate HRRN registration')

def render(node, output, scheduler_image, run='basic1111'):
    if node not in NODES:
        raise ValueError('Unknown reviewed node')
    if '@sha256:' not in scheduler_image or len(scheduler_image.rsplit(':', 1)[1]) != 64:
        raise ValueError('Immutable scheduler image required')
    output.mkdir(parents=True, exist_ok=False)
    # Isolated reuse of the stage5 pure generator; none of these globals is an
    # online config, and the old runner cannot execute this new bundle.
    old.NODE_NAME = node
    old.SYSTEM_NS = 'hrrn-basic-system'
    old.TEST_NS = 'hrrn-basic-tests'
    old.SCHEDULER_NAME = 'volcano-hrrn-basic'
    old.SERVICE_ACCOUNT = 'volcano-hrrn-basic'
    old.QUEUE_NAME = 'hrrn-basic-queue'
    old.EXPERIMENT = 'hrrn-basic'
    old.SCHEDULER_IMAGE = scheduler_image
    configs = configurations()
    files = {}
    def save(name, value):
        data = (json.dumps(value, indent=2, ensure_ascii=False)+'\n').encode()
        target = output/name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        files[name] = hashlib.sha256(data).hexdigest()
    for mode, config in configs.items():
        validate_config(config, mode)
        name = 'hrrn-basic-'+run+'-'+mode.lower()
        cm = {'apiVersion':'v1','kind':'ConfigMap','metadata':{'name':name,'namespace':old.SYSTEM_NS},
              'data':{'volcano-scheduler.conf':json.dumps(config,indent=2)+'\n'}}
        dep = old.scheduler_deployment(run, mode)
        dep['spec']['template']['spec']['volumes'][0]['configMap']['name'] = name
        save('config-'+mode+'.json', cm)
        save('deployment-'+mode+'-replicas0.json', dep)
    cases = []
    for kind in ('order', 'bypass'):
        jobs = {}
        for i in range(NODES[node][2]-1):
            jobs['blocker-'+str(i)] = old.standalone_job(run, kind, 'blocker-'+str(i), 100, 300, 300)
        if kind == 'order':
            jobs['hold'] = old.standalone_job(run, kind, 'hold', 100, 300, 300)
            jobs['long'] = old.standalone_job(run, kind, 'long', 100, 100, 45)
            jobs['short'] = old.standalone_job(run, kind, 'short', 100, 10, 45)
        else:
            jobs['background'] = old.standalone_job(run, kind, 'background', 30, 300, 300)
            jobs['large'] = old.standalone_job(run, kind, 'large', 80, 10, 45)
            jobs['small'] = old.standalone_job(run, kind, 'small', 60, 100, 45)
        for role, objects in jobs.items():
            assert len([o for o in objects if o['kind']=='Pod']) == 1
            save('jobs/'+kind+'/'+role+'.json', {'apiVersion':'v1','kind':'List','items':objects})
        cases.append({'kind':kind,'modes':['A','B'],'input_identical_between_modes':True,
                      'roles':list(jobs),'minimum_observed_wait_seconds':20,
                      'basis':'A: external complete-membership observation (no HRRN tracker); B: same-process HRRN tracker; neither substitutes creationTime or an injected clock',
                      'expected_first':{'A':'long','B':'short'} if kind=='order' else {'A':'small','B':'small'},
                      'contribution':'HRRN sorting' if kind=='order' else 'native feasibility bypass control'})
    save('protocol.json', {'node':node,'historical_hardware':NODES[node],'cases':cases,
        'execution_enabled':False,'reason':'Fresh API/runtime/RBAC/image/quota/exclusivity and restore review required',
        'multi_pod_enabled':False,'rf_image':old.RF_IMAGE,'scheduler_image':scheduler_image,
        'metadata_propagation_case':old.propagation_job(run),
        'required_evidence':['same-process readyAt and waiting','Priority/Gang/DRF ties','actual bind and Running',
                             'RF/Allocate/plugin UUID','completion and release','original scheduler restoration']})
    save('manifest.json',{'files':files.copy(),'deployed':False,'node':node})
    return {'node':node,'output':str(output),'files':len(files),'deployed':False}

if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--node',choices=list(NODES),required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--scheduler-image',required=True)
    a=p.parse_args()
    print(json.dumps(render(a.node,a.output,a.scheduler_image),ensure_ascii=False))
