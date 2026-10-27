#!/usr/bin/env python3
"""Read-only collector: no kubectl mutations, CUDA execution or remote file writes.

Run on each GPU host as python3 audit_vgpu_readonly.py. Management host can use
--cluster --kubeconfig /etc/kubernetes/admin.conf --sudo-kubectl.
JSONL stdout preserves commands, timestamps and failures. Never reads Secrets.
"""
import argparse
import datetime as dt
import glob
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess


def emit(kind, **values):
    print(json.dumps(dict(kind=kind, utc=dt.datetime.now(dt.timezone.utc).isoformat(),
                          host=socket.gethostname(), **values), ensure_ascii=False), flush=True)


def run(name, argv, project=None, timeout=20):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        value = r.stdout
        if project and r.returncode == 0:
            value = project(json.loads(r.stdout))
        emit(name, command=argv, exit_code=r.returncode, output=value, stderr=r.stderr,
             raw_stdout_sha256=hashlib.sha256(r.stdout.encode()).hexdigest())
        return r.stdout if r.returncode == 0 else None
    except Exception as e:
        emit(name, command=argv, error=type(e).__name__ + ': ' + str(e))
        return None


def pod_projection(data):
    rows = []
    for p in data.get('items', []):
        m, s, st = p['metadata'], p['spec'], p.get('status', {})
        containers = s.get('containers', []) + s.get('initContainers', [])
        relevant = m['namespace'] == 'volcano-system' or any(
            any('gpu' in k.lower() for k in c.get('resources', {}).get('limits', {})) for c in containers
        ) or any('gpu' in k.lower() or 'devices-to-allocate' in k for k in m.get('annotations', {}))
        if not relevant:
            continue
        rows.append(dict(namespace=m['namespace'], name=m['name'], uid=m['uid'],
            phase=st.get('phase'), deletionTimestamp=m.get('deletionTimestamp'),
            node=s.get('nodeName'), scheduler=s.get('schedulerName'),
            labels=m.get('labels', {}), annotations={k:v for k,v in m.get('annotations', {}).items()
                if any(x in k.lower() for x in ('volcano', 'gpu', 'hami'))},
            containers=[dict(name=c['name'], image=c.get('image'), resources=c.get('resources', {}))
                        for c in containers], containerStatuses=st.get('containerStatuses', [])))
    return rows


def controllers(data):
    rows = []
    for x in data.get('items', []):
        t = x['spec'].get('template', {}).get('spec', {})
        rows.append(dict(kind=x['kind'], name=x['metadata']['name'], namespace=x['metadata']['namespace'],
            containers=[{k:c[k] for k in ('name','image','command','args','volumeMounts') if k in c}
                        for c in t.get('containers', [])],
            volumes=t.get('volumes', []), status=x.get('status', {})))
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--cluster', action='store_true')
    ap.add_argument('--kubeconfig')
    ap.add_argument('--sudo-kubectl', action='store_true')
    a = ap.parse_args()
    emit('metadata', read_only=True, creates_pods=False, executes_cuda=False)
    run('gpu_inventory', ['nvidia-smi', '--query-gpu=index,uuid,name,memory.total,memory.used,memory.free,utilization.gpu,driver_version', '--format=csv,nounits'])
    processes = run('gpu_compute_processes', ['nvidia-smi', '--query-compute-apps=gpu_uuid,pid,process_name,used_gpu_memory', '--format=csv,nounits'])
    if processes:
        import csv, io
        for row in list(csv.reader(io.StringIO(processes)))[1:101]:
            if len(row) < 2 or not row[1].strip().isdigit():
                continue
            pid = row[1].strip()
            # Only GPU-associated processes; do not collect argv/environ credentials.
            for fname in ('cgroup','comm','status','maps'):
                p = '/proc/' + pid + '/' + fname
                raw = subprocess.run(['sudo','-n','cat',p], capture_output=True, text=True)
                output = raw.stdout
                if fname == 'maps':
                    output = '\n'.join(x for x in output.splitlines() if any(y in x.lower() for y in ('vgpu','hami')))
                if fname == 'status':
                    output = '\n'.join(x for x in output.splitlines() if x.startswith(('Name:', 'Uid:', 'Gid:', 'PPid:', 'NSpid:')))
                emit('process_ownership', pid=int(pid), path=p, exit_code=raw.returncode, output=output, stderr=raw.stderr)
    for pattern in ('/usr/local/vgpu/*.so*','/usr/local/vgpu/lib/*.so*','/opt/hami/lib/*.so*'):
        for p in glob.glob(pattern):
            run('hami_library_hash', ['sudo','-n','sha256sum',p])
    if not a.cluster:
        return
    kubectl = (['sudo','-n'] if a.sudo_kubectl else []) + ['kubectl']
    if a.kubeconfig:
        kubectl += ['--kubeconfig=' + a.kubeconfig]
    run('kubernetes_version', kubectl + ['version','-o','json','--request-timeout=12s'])
    run('nodes', kubectl + ['get','nodes','-o','json','--request-timeout=12s'], lambda d: [
        dict(name=n['metadata']['name'], annotations={k:v for k,v in n['metadata'].get('annotations',{}).items()
            if any(t in k.lower() for t in ('gpu','hami','volcano'))},
            capacity=n['status'].get('capacity',{}), allocatable=n['status'].get('allocatable',{}),
            nodeInfo=n['status'].get('nodeInfo',{})) for n in d.get('items',[])])
    run('gpu_pods', kubectl + ['get','pods','-A','-o','json','--request-timeout=12s'], pod_projection)
    run('volcano_controllers', kubectl + ['get','deployments,daemonsets','-n','volcano-system','-o','json','--request-timeout=12s'], controllers)
    run('volcano_configmaps', kubectl + ['get','configmaps','-n','volcano-system','-o','json','--request-timeout=12s'],
        lambda d: [dict(name=c['metadata']['name'], data=c.get('data',{})) for c in d.get('items',[])])
    run('plugin_logs', kubectl + ['logs','-n','volcano-system','-l','name=volcano-vgpu-device-plugin','--all-containers=true','--tail=160','--prefix=true','--request-timeout=12s'])
    run('scheduler_logs', kubectl + ['logs','-n','volcano-system','-l','app=volcano-scheduler','--tail=120','--prefix=true','--request-timeout=12s'])
    binary = '/home/zbs/volcano-v1.13.1-dqn/_output/bin/vc-scheduler'
    run('custom_binary_hash', ['sudo','-n','sha256sum',binary])
    # Read source only, including backend implementations missing in the local audit.
    checkout = Path('/home/zbs/volcano-v1.13.1-dqn')
    relative = ['go.mod', 'pkg/scheduler/api/devices/nvidia/vgpu/device_info.go',
                'pkg/scheduler/api/devices/nvidia/vgpu/gpushare.go',
                'pkg/scheduler/api/devices/nvidia/vgpu/gpu_info.go',
                'pkg/scheduler/api/devices/nvidia/vgpu/share_mode.go',
                'pkg/scheduler/api/devices/nvidia/vgpu/hamishare.go',
                'pkg/scheduler/api/devices/nvidia/vgpu/utils.go',
                'pkg/scheduler/api/devices/nvidia/vgpu/dqn_client.go',
                'pkg/scheduler/plugins/deviceshare/deviceshare.go']
    run('source_file_inventory', ['git','-C',str(checkout),'ls-files','pkg/scheduler/api/devices/nvidia/vgpu'])
    run('source_git_head', ['git','-C',str(checkout),'rev-parse','HEAD'])
    for p in relative:
        path = checkout / p
        if path.is_file():
            run('custom_source', ['cat', str(path)])


if __name__ == '__main__':
    main()
