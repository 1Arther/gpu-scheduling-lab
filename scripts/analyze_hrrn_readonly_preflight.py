"""Offline evidence analysis of the fixed, read-only Kubernetes export.

Does not contain a cluster client or infer physical GPU idleness from quotas.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gpu_lab.staging import inventory
from round7_window_preflight import blockers, gpu_demand


def analyze(path, target):
    raw = path.read_bytes()
    objects = json.loads(raw)['items']
    bykind = {kind: [o for o in objects if o['kind'] == kind]
              for kind in ('Node', 'Pod', 'Job', 'PodGroup', 'Queue')}
    nodes, pods = bykind['Node'], bykind['Pod']
    devices, warnings = inventory(nodes, pods)
    pg_rows = []
    for group in bykind['PodGroup']:
        m = group['metadata']
        members = [p for p in pods if p['metadata']['namespace'] == m['namespace']
                   and p['metadata'].get('annotations', {}).get('scheduling.k8s.io/group-name') == m['name']]
        pg_rows.append(dict(namespace=m['namespace'], name=m['name'], uid=m['uid'],
            phase=group.get('status', {}).get('phase'), member_count=len(members),
            active_members=sum(p.get('status', {}).get('phase') not in ('Succeeded', 'Failed') for p in members)))
    components = []
    for p in pods:
        m = p['metadata']
        if not any(s in m['name'] for s in ('scheduler', 'device-plugin')):
            continue
        components.append(dict(namespace=m['namespace'], name=m['name'], uid=m['uid'],
            node=p['spec'].get('nodeName'), phase=p.get('status', {}).get('phase'),
            container_readiness=[dict(name=c['name'], ready=c.get('ready'), image_id=c.get('imageID'))
                                 for c in p.get('status', {}).get('containerStatuses', [])],
            node_ready=next((any(c['type'] == 'Ready' and c['status'] == 'True'
                             for c in n['status']['conditions']) for n in nodes
                            if n['metadata']['name'] == p['spec'].get('nodeName')), None)))
    return dict(input_file=str(path), input_sha256=hashlib.sha256(raw).hexdigest(),
        scope='One sequential multi-resource Kubernetes GET export, not an atomic cluster snapshot',
        counts={kind: len(items) for kind, items in bykind.items()},
        nodes=[dict(name=n['metadata']['name'], ready=any(c['type'] == 'Ready' and c['status'] == 'True'
                    for c in n['status']['conditions']), unschedulable=n['spec'].get('unschedulable', False),
                    node_lock=n['metadata'].get('annotations', {}).get('hamivgpu')) for n in nodes],
        target_node=target, target_logical_devices=[d for d in devices if d['node'] == target],
        annotation_warnings=warnings, target_blockers=blockers(pods, target),
        gpu_pod_phase_counts=dict(Counter(p['status'].get('phase') for p in pods if gpu_demand(p))),
        volcano_jobs=[dict(namespace=j['metadata']['namespace'], name=j['metadata']['name'], uid=j['metadata']['uid'],
                           phase=j.get('status', {}).get('state', {}).get('phase')) for j in bykind['Job']],
        podgroups=pg_rows, components=components,
        queue_states=[dict(name=q['metadata']['name'], state=q.get('status', {}).get('state')) for q in bykind['Queue']],
        cluster_resources_modified=False, real_hrrn_experiment_executed=False,
        unknowns=['Host GPU processes and physical occupancy', 'Scheduler-private pending reservations',
                  'Current Deployment, ConfigMap, ResourceQuota and RBAC contents',
                  'Controller templates and DRA ResourceClaims not included in the fixed GET',
                  'Server RF model and running scheduler executable SHA', 'Container runtime image cache and Device Plugin checkpoint',
                  'Human ownership of unlabelled Jobs; SSH username is not proof of submission identity'],
        boundary='This partial preflight does not authorize a scheduler switch, job termination, or declaration of GPU exclusivity')


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--input', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True)
    ap.add_argument('--node', default='t3dgq')
    args = ap.parse_args()
    result = analyze(args.input, args.node)
    # Exclusive creation preserves prior evidence rather than overwriting it.
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    print(json.dumps(dict(counts=result['counts'], warnings=len(result['annotation_warnings']),
                         active_gpu_or_dra_pods=len(result['target_blockers']['active_gpu_or_DRA_demand']))))
