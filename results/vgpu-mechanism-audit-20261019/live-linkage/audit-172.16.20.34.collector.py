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
import urllib.request


def final_evidence(a):
    emit('metadata', read_only=True, creates_pods=False, executes_cuda=False, final_evidence=True)
    run('gpu_physical_reserved', ['nvidia-smi','--query-gpu=uuid,memory.total,memory.used,memory.free,memory.reserved,utilization.gpu','--format=csv,nounits'])
    emit('host_preload_environment', LD_PRELOAD=os.environ.get('LD_PRELOAD'), CUDA_DEVICE_MEMORY_LIMIT=os.environ.get('CUDA_DEVICE_MEMORY_LIMIT'))
    if Path('/etc/ld.so.preload').is_file():run('host_preload_file',['cat','/etc/ld.so.preload'])
    raw=subprocess.run(['sudo','-n','crictl','ps','-o','json'],capture_output=True,text=True,timeout=20)
    if raw.returncode == 0:
        for c in json.loads(raw.stdout).get('containers',[]):
            name=c.get('metadata',{}).get('name','')
            if not any(t in name.lower() for t in ('device-plugin','vgpu','monitor')):continue
            x=subprocess.run(['sudo','-n','crictl','inspect',c['id']],capture_output=True,text=True,timeout=20)
            if x.returncode!=0:emit('cri_inspect_failed',name=name,stderr=x.stderr);continue
            d=json.loads(x.stdout); info=d.get('info',{}); spec=info.get('runtimeSpec',{})
            emit('plugin_cri_runtime', name=name, id=c['id'], pid=info.get('pid'),
                 image=d.get('status',{}).get('image'),imageRef=d.get('status',{}).get('imageRef'),
                 args=spec.get('process',{}).get('args'),
                 env=[v for v in spec.get('process',{}).get('env',[]) if v.startswith(('NODE_NAME=','HOOK_PATH=','CUDA_DEVICE_','NVIDIA_VISIBLE_DEVICES=','GPU_','DEVICE_'))],
                 mounts=[m for m in spec.get('mounts',[]) if any(t in m.get('destination','').lower() for t in ('vgpu','hami','device-plugins'))],
                 labels={k:v for k,v in d.get('status',{}).get('labels',{}).items() if k.startswith('io.kubernetes.')})
    else:emit('crictl_failed',stderr=raw.stderr)
    if not a.cluster:return
    k=(['sudo','-n'] if a.sudo_kubectl else [])+['kubectl','--kubeconfig='+a.kubeconfig]
    rawpods=run('plugin_pods_exact', k+['get','pods','-A','-o','json','--request-timeout=12s'],lambda d:[
        dict(name=p['metadata']['name'],namespace=p['metadata']['namespace'],labels=p['metadata'].get('labels'),
             node=p['spec'].get('nodeName'),podIP=p['status'].get('podIP'),containers=p['spec']['containers'],
             containerStatuses=p['status'].get('containerStatuses',[])) for p in d.get('items',[]) if 'device-plugin' in p['metadata']['name']])
    run('plugin_daemonsets_exact',k+['get','daemonsets','-A','-o','json','--request-timeout=12s'],lambda d:controllers({'items':[x for x in d.get('items',[]) if 'device-plugin' in x['metadata']['name']]}))
    if rawpods:
        for p in json.loads(rawpods).get('items',[]):
            if 'device-plugin' not in p['metadata']['name'] or 'volcano' not in p['metadata']['name']:continue
            ns=p['metadata']['namespace']; name=p['metadata']['name']; ip=p['status'].get('podIP')
            for c in p['spec']['containers']:
                run('plugin_pod_existing_logs',k+['logs','-n',ns,name,'-c',c['name'],'--tail=80','--request-timeout=12s'])
            if ip:
                u='http://'+ip+':9394/metrics'
                try:
                    with urllib.request.urlopen(u,timeout=5) as response:text=response.read(4*1024*1024).decode()
                    emit('monitor_metrics_exact',node=p['spec'].get('nodeName'),url=u,output='\n'.join(l for l in text.splitlines() if any(t in l for t in ('HostGPU','HostCore','vGPU_','Device_memory','Device_utilization'))),raw_stdout_sha256=hashlib.sha256(text.encode()).hexdigest())
                except Exception as e:emit('monitor_metrics_exact',url=u,error=str(e))


def linkage(a):
    emit('metadata', read_only=True, creates_pods=False, executes_cuda=False, linkage=True)
    run('hami_host_library_hash',['sudo','-n','sha256sum','/usr/local/vgpu/libvgpu.so'])
    # Compare the image's embedded library with the injected host copy, without exec.
    rawps=subprocess.run(['ps','-eo','pid,comm'],capture_output=True,text=True)
    for row in rawps.stdout.splitlines():
        f=row.split()
        if len(f)==2 and f[1].startswith('volcano-vgpu-de'):
            run('hami_image_library_hash',['sudo','-n','sha256sum','/proc/'+f[0]+'/root/k8s-vgpu/lib/nvidia/libvgpu.so'])
    if not a.cluster:return
    checkout='/home/zbs/volcano-v1.13.1-dqn/'
    for p in ('pkg/scheduler/cache/factory.go','pkg/scheduler/cache/interface.go','pkg/scheduler/cache/util.go',
              'pkg/scheduler/framework/session.go','pkg/scheduler/api/shared_device_pool.go',
              'pkg/scheduler/api/devices/config/config.go'):
        run('custom_source',['cat',checkout+p])
    run('scheduler_go_build_info',['sudo','-n','/usr/bin/go','version','-m','/proc/1978316/exe'])


def supplement(a):
    """Read existing standalone plugin containers and known metrics endpoints."""
    emit('metadata', read_only=True, creates_pods=False, executes_cuda=False, supplement=True)
    rawps = subprocess.run(['ps','-eo','pid,ppid,user,comm'], capture_output=True, text=True)
    selected = [l for l in rawps.stdout.splitlines() if any(t in l.lower() for t in ('vgpu','nvidia-device','hami','monitor'))]
    emit('plugin_processes', command=['ps','-eo','pid,ppid,user,comm'], exit_code=rawps.returncode, output='\n'.join(selected), stderr=rawps.stderr)
    for line in selected:
        fields=line.split()
        if not fields or not fields[0].isdigit():continue
        pid=fields[0]
        for name in ('cmdline','cgroup'):
            run('plugin_proc_'+name,['sudo','-n','cat','/proc/'+pid+'/'+name])
        run('plugin_process_binary_hash',['sudo','-n','sha256sum','/proc/'+pid+'/exe'])
        import shutil
        go=shutil.which('go')
        if go:run('plugin_process_go_build_info',['sudo','-n',go,'version','-m','/proc/'+pid+'/exe'])
    rawsock=subprocess.run(['sudo','-n','ss','-xlpn'],capture_output=True,text=True)
    emit('plugin_sockets', command=['sudo','-n','ss','-xlpn'], exit_code=rawsock.returncode,
         output='\n'.join(l for l in rawsock.stdout.splitlines() if any(t in l.lower() for t in ('vgpu','nvidia','device-plugins'))),stderr=rawsock.stderr)
    run('listening_ports', ['sudo','-n','ss','-lntp'])
    raw = subprocess.run(['sudo','-n','docker','ps','--format','{{json .}}'], capture_output=True, text=True)
    emit('docker_ps', exit_code=raw.returncode, output=raw.stdout, stderr=raw.stderr)
    if raw.returncode == 0:
        for line in raw.stdout.splitlines():
            c=json.loads(line)
            if not any(t in (c.get('Image','')+' '+c.get('Names','')).lower() for t in ('vgpu','hami')):
                continue
            x=subprocess.run(['sudo','-n','docker','inspect',c['ID']], capture_output=True, text=True)
            if x.returncode != 0:
                emit('docker_inspect_failed', stderr=x.stderr);continue
            d=json.loads(x.stdout)[0]; config=d.get('Config',{}); pid=d.get('State',{}).get('Pid')
            emit('plugin_docker_runtime', id=d['Id'], name=d['Name'], image=d.get('Image'),
                 config_image=config.get('Image'), cmd=config.get('Cmd'), entrypoint=config.get('Entrypoint'),
                 env=[v for v in config.get('Env',[]) if v.startswith(('NODE_NAME=','HOOK_PATH=','CUDA_DEVICE_','NVIDIA_VISIBLE_DEVICES=','GPU_','DEVICE_'))],
                 mounts=d.get('Mounts'), networkMode=d.get('HostConfig',{}).get('NetworkMode'),
                 networks=d.get('NetworkSettings',{}).get('Networks'), pid=pid)
            if pid:
                run('plugin_executable_hash',['sudo','-n','sha256sum','/proc/'+str(pid)+'/exe'])
                # Reads Go build metadata, does not execute the plugin.
                import shutil
                go=shutil.which('go')
                if go:run('plugin_go_build_info',['sudo','-n',go,'version','-m','/proc/'+str(pid)+'/exe'])
            run('plugin_existing_logs',['sudo','-n','docker','logs','--tail','120',c['ID']])
    # The exact monitor port is confirmed in upstream v1.11.0 metrics.go.
    urls=['http://127.0.0.1:9394/metrics']
    if a.cluster:
        urls += ['http://10.244.0.147:8080/metrics']
        # Existing Pod IP from the immediately preceding read-only collection.
        # A failed request never enables metrics or changes scheduler flags.
    for u in urls:
        try:
            with urllib.request.urlopen(u, timeout=5) as response:
                text=response.read(4*1024*1024).decode('utf-8',errors='replace')
            emit('metrics_read', url=u, output='\n'.join(l for l in text.splitlines() if any(t in l for t in ('vgpu','HostGPU','HostCore','vGPU','Device_memory','Device_utilization'))), raw_stdout_sha256=hashlib.sha256(text.encode()).hexdigest())
        except Exception as e:emit('metrics_read', url=u, error=str(e))
    if a.cluster:
        # Existing cache files only; never trigger a signal or write a dump.
        run('scheduler_existing_cache_files',['sudo','-n','find','/proc/1978316/root/tmp','-maxdepth','1','-type','f','-printf','%f %s bytes\n'])
        for p in ('cmd/scheduler/app/options/options.go', 'pkg/scheduler/api/devices/config/config.go'):
            run('custom_source',['cat','/home/zbs/volcano-v1.13.1-dqn/'+p])
        run('dqn_service_source',['cat','/home/zbs/vGPU-DQN/DQN2/grpc/dqn_scheduler_server.py'])


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
        relevant = m['namespace'] == 'volcano-system' or any(t in m['name'].lower() for t in ('vgpu','nvidia')) or any(
            any('gpu' in k.lower() for k in c.get('resources', {}).get('limits', {})) for c in containers
        ) or any('gpu' in k.lower() or 'devices-to-allocate' in k for k in m.get('annotations', {}))
        if not relevant:
            continue
        rows.append(dict(namespace=m['namespace'], name=m['name'], uid=m['uid'],
            phase=st.get('phase'), deletionTimestamp=m.get('deletionTimestamp'),
            node=s.get('nodeName'), scheduler=s.get('schedulerName'), podIP=st.get('podIP'), hostNetwork=s.get('hostNetwork'),
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
    ap.add_argument('--supplement', action='store_true')
    ap.add_argument('--final-evidence', action='store_true')
    ap.add_argument('--linkage', action='store_true')
    a = ap.parse_args()
    if a.linkage:
        linkage(a); return
    if a.final_evidence:
        final_evidence(a); return
    if a.supplement:
        supplement(a); return
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
                'pkg/scheduler/api/devices/nvidia/vgpu/hamicore.go',
                'pkg/scheduler/api/devices/nvidia/vgpu/type.go',
                'pkg/scheduler/api/devices/nvidia/vgpu/metrics.go',
                'pkg/scheduler/api/node_info.go',
                'pkg/scheduler/cache/event_handlers.go',
                'pkg/scheduler/cache/cache.go',
                'pkg/scheduler/framework/statement.go',
                'pkg/scheduler/api/devices/nvidia/vgpu/utils.go',
                'pkg/scheduler/api/devices/nvidia/vgpu/dqn_client.go',
                'pkg/scheduler/plugins/deviceshare/deviceshare.go']
    run('source_file_inventory', ['git','-C',str(checkout),'ls-files','pkg/scheduler/api/devices/nvidia/vgpu'])
    run('source_git_head', ['git','-C',str(checkout),'rev-parse','HEAD'])
    for p in relative:
        path = checkout / p
        if path.is_file():
            run('custom_source', ['cat', str(path)])
    # Plugins may be installed in kube-system rather than volcano-system.
    run('gpu_daemonsets', kubectl + ['get','daemonsets','-A','-o','json','--request-timeout=12s'],
        lambda d: controllers({'items':[x for x in d.get('items',[]) if any(t in x['metadata']['name'].lower() for t in ('gpu','nvidia','hami'))]}))
    # Existing hostPath source, not execution of the deployed DQN service.
    run('dqn_service_source', ['cat','/home/zbs/vGPU-DQN/DQN2/grpc/dqn_scheduler_server.py'])
    run('listening_ports', ['sudo','-n','ss','-lntp'])
    # Inspect only relevant existing containers and project their safe fields.
    raw = subprocess.run(['sudo','-n','crictl','ps','-o','json'], capture_output=True, text=True)
    if raw.returncode == 0:
        for c in json.loads(raw.stdout).get('containers',[]):
            name = c.get('metadata',{}).get('name','')
            if not any(t in name.lower() for t in ('vgpu','volcano-scheduler')):
                continue
            x = subprocess.run(['sudo','-n','crictl','inspect',c['id']], capture_output=True, text=True)
            if x.returncode != 0:
                emit('container_inspect_failed', name=name, stderr=x.stderr); continue
            d = json.loads(x.stdout); info=d.get('info',{}); runtime=info.get('runtimeSpec',{})
            safe_env=[v for v in runtime.get('process',{}).get('env',[]) if v.startswith(('CUDA_DEVICE_','NVIDIA_VISIBLE_DEVICES=','LD_PRELOAD=','HOOK_PATH=','GPUMEMORY_FACTOR='))]
            pid=info.get('pid')
            emit('existing_container_runtime', name=name, container_id=c['id'], pid=pid,
                 image=d.get('status',{}).get('image'), imageRef=d.get('status',{}).get('imageRef'),
                 mounts=[m for m in runtime.get('mounts',[]) if any(t in m.get('destination','').lower() for t in ('vgpu','hami','vc-scheduler'))], env=safe_env)
            if pid:
                run('running_executable_hash', ['sudo','-n','sha256sum','/proc/'+str(pid)+'/exe'])
    else:
        emit('crictl_unavailable', exit_code=raw.returncode, stderr=raw.stderr)


if __name__ == '__main__':
    main()
