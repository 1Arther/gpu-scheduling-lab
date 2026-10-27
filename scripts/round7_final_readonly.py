"""Additional GET/file-read evidence. No signals, exec, writes, RPC predictions or startup.

Run by piping to python3 on the management node. Existing cache dump filenames
are inspected, never generated. A quiet log is not a private-ledger dump.
"""
import datetime as dt
import hashlib
import json
import re
import subprocess


def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


def run(ident, command, project=None):
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    try:
        p = subprocess.run(command, capture_output=True, text=True, timeout=30)
        row = dict(id=ident, utc=now, command=command, exit_code=p.returncode,
                   stderr=p.stderr[:1500], sha256=hashlib.sha256(p.stdout.encode()).hexdigest())
        if p.returncode == 0 and project:
            row['data'] = project(p.stdout)
        else:
            row['stdout'] = p.stdout[:30000]
        emit(row)
        return row.get('data')
    except Exception as exc:
        emit(dict(id=ident, utc=now, command=command, error=str(exc)))


K = ['sudo', '-n', 'kubectl', '--kubeconfig=/etc/kubernetes/admin.conf']


def demand(spec):
    out = []
    for c in spec.get('containers', []) + spec.get('initContainers', []):
        for field in ('requests', 'limits'):
            for key, value in c.get('resources', {}).get(field, {}).items():
                if key.startswith(('nvidia.com/', 'volcano.sh/vgpu-')):
                    out.append(dict(container=c['name'], field=field, resource=key, quantity=value))
    if spec.get('resourceClaims'):
        out.append(dict(resourceClaims=spec['resourceClaims']))
    return out


def templates(raw):
    rows = []
    for o in json.loads(raw)['items']:
        s = o.get('spec', {})
        t = s.get('jobTemplate', {}).get('spec', s).get('template', {}).get('spec', {})
        d = demand(t)
        if d:
            m = o['metadata']
            rows.append(dict(kind=o['kind'], namespace=m.get('namespace'), name=m['name'],
                             uid=m['uid'], replicas=s.get('replicas'), suspend=s.get('suspend'),
                             schedule=s.get('schedule'), schedulerName=t.get('schedulerName'),
                             nodeSelector=t.get('nodeSelector'), demand=d, status=o.get('status')))
    return rows


def log_summary(raw):
    lines = raw.splitlines()
    patterns = {'cycle_end': r'End scheduling', 'dqn_prediction': r'DQN(Job)?Policy',
                'device_add_or_sub': r'(?:add|sub) Pod:',
                'bind_or_allocate': r'(?:After allocated Task|DeviceSharing:Allocate Success|bind ok, latency|Failed to bind pod|Failed to allocate vgpu task)',
                'empty_gpu_podgroups_skipped': r'Job <default/pg-gpu-.*skip allocate.*valid: 0',
                'cache_dump': r'Dump of nodes|dump info in scheduler cache|snapshot-'}
    counts = {k: sum(bool(re.search(v, x, re.I)) for x in lines) for k, v in patterns.items()}
    relevant = [x for x in lines if any(re.search(v, x, re.I) for k, v in patterns.items() if k != 'cycle_end')]
    return dict(line_count=len(lines), first_line=lines[0][:250] if lines else None,
                last_line=lines[-1][:250] if lines else None, counts=counts, relevant_lines=relevant[-60:],
                evidence_limit='Bounded existing log only; absence of matching lines does not prove an empty private ledger.')


emit(dict(id='meta', utc=dt.datetime.now(dt.timezone.utc).isoformat(), read_only=True,
          cluster_resources_changed=False, sends_process_signals=False))
run('gpu_controller_templates', K + ['get', 'deployments,statefulsets,daemonsets,replicasets,replicationcontrollers,jobs,cronjobs',
    '-A', '-o', 'json', '--request-timeout=15s'], templates)
run('dra_claims', K + ['get', 'resourceclaims.resource.k8s.io', '-A', '-o', 'json', '--request-timeout=15s'],
    lambda raw: [dict(namespace=o['metadata'].get('namespace'), name=o['metadata']['name'], uid=o['metadata']['uid'],
                      reservedFor=o.get('status', {}).get('reservedFor'), allocation=o.get('status', {}).get('allocation'))
                 for o in json.loads(raw)['items']])
run('dra_slices', K + ['get', 'resourceslices.resource.k8s.io', '-o', 'json', '--request-timeout=15s'],
    lambda raw: [dict(name=o['metadata']['name'], driver=o.get('spec', {}).get('driver'),
                      nodeName=o.get('spec', {}).get('nodeName'), device_count=len(o.get('spec', {}).get('devices', [])))
                 for o in json.loads(raw)['items']])
run('old_scheduler_recent_log', K + ['logs', '-n', 'volcano-system', 'deployment/volcano-scheduler',
    '--since=60s', '--limit-bytes=8000000', '--timestamps', '--request-timeout=15s'], log_summary)
containers = run('scheduler_cri', ['sudo', '-n', 'crictl', 'ps', '-o', 'json'],
    lambda raw: [dict(id=c['id'], metadata=c.get('metadata')) for c in json.loads(raw).get('containers', [])
                 if c.get('metadata', {}).get('name') == 'volcano-scheduler']) or []
for c in containers:
    info = run('scheduler_process', ['sudo', '-n', 'crictl', 'inspect', '-o', 'json', c['id']],
        lambda raw: {k: json.loads(raw).get('info', {}).get(k) for k in ('pid',)})
    if info and isinstance(info.get('pid'), int) and info['pid'] > 0:
        base = '/proc/' + str(info['pid']) + '/root'
        run('existing_cache_dump_files', ['sudo', '-n', 'find', base + '/tmp', '-maxdepth', '1', '-type', 'f',
            '-name', 'snapshot-*.json', '-printf', '%f %s %TY-%Tm-%TdT%TH:%TM:%TS\n'])
        run('running_scheduler_executable_sha', ['sudo', '-n', 'sha256sum', '/proc/' + str(info['pid']) + '/exe'])
run('deployed_source_sha', ['sha256sum', '/home/zbs/volcano-v1.13.1-dqn/pkg/scheduler/api/devices/nvidia/vgpu/dqn_client.go',
    '/home/zbs/volcano-v1.13.1-dqn/pkg/scheduler/cache/dumper.go'])
run('isolated_artifacts', ['sha256sum', '/home/zbs/rf-round7-reassess-20261019/results/reassess-release-cpu/vc-scheduler',
    '/home/zbs/rf-round7-reassess-20261019/models/pod-gpu-rf-v1/random_forest.joblib'])
