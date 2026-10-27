#!/usr/bin/env python3
"""Replay captured read-only output; no SSH, API requests, GPU calls or ML."""
import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import re


def csv_rows(text):
    return [{k.strip():v.strip() for k,v in r.items()} for r in csv.DictReader(io.StringIO(text))]


def allocated_devices(annotation):
    """Actual scheduler encoding: container groups ';', devices ':', fields ','."""
    result=[]
    for container in annotation.split(';'):
        for token in container.split(':'):
            if not token.strip():continue
            f=token.split(',')
            if len(f)!=4:raise ValueError('Invalid allocated device annotation: '+token)
            result.append(dict(uuid=f[0], type=f[1], memory=int(f[2]), cores=int(f[3])))
    return result


def reconstruct_ledger(pods):
    ledger={}; unresolved=[]
    for p in pods:
        if p.get('phase') in ('Succeeded','Failed'):continue
        annotation=p.get('annotations',{}).get('volcano.sh/vgpu-ids-new','')
        has_request=any(any(k.startswith(('volcano.sh/vgpu-','nvidia.com/gpu')) for k in c.get('resources',{}).get('limits',{})) for c in p.get('containers',[]))
        if not annotation:
            if has_request:unresolved.append(dict(namespace=p['namespace'],name=p['name'],node=p.get('node'),reason='active GPU request without UUID allocation annotation'))
            continue
        for d in allocated_devices(annotation):
            row=ledger.setdefault(d['uuid'],dict(memory=0,cores=0,count=0,pods=[]))
            row['memory']+=d['memory'];row['cores']+=d['cores'];row['count']+=1
            row['pods'].append(p['namespace']+'/'+p['name'])
    return ledger,unresolved


def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--evidence',required=True);a=ap.parse_args()
    root=Path(a.evidence).resolve();out=root/'summary';out.mkdir(exist_ok=True)
    records=[]
    for dirname in ('live-verified','live-details','live-plugin-metrics','live-process-verification','live-final-evidence','live-linkage','live-bind-callback'):
        for f in (root/dirname).glob('*.jsonl'):
            for n,line in enumerate(f.read_text(encoding='utf-8').splitlines(),1):
                r=json.loads(line);r['_file']=str(f.relative_to(root));r['_line']=n;records.append(r)
    mgmt=[r for r in records if r['kind']=='gpu_pods'][-1]
    pods=mgmt['output'];ledger,unresolved=reconstruct_ledger(pods)
    node_records=[r for r in records if r['kind']=='nodes'][-1]
    registered={}
    for node in node_records['output']:
        if node['name'] not in ('t3dgq','t3dmg','t3dgq-2'):continue
        for token in node['annotations'].get('volcano.sh/node-vgpu-register','').split(':'):
            if not token:continue
            f=token.split(',');registered[f[0]]=dict(node=node['name'],max_instances=int(f[1]),registered_memory=int(f[2]),health=f[4],mode=f[5])
    # Use the final reserved-memory query for the comparison table; retain timestamps.
    inventories={r['host'].lower():r for r in records if r['kind']=='gpu_inventory'}
    latest={r['host'].lower():r for r in records if r['kind']=='gpu_physical_reserved' and r.get('exit_code')==0}
    table=[]
    for host,inv in inventories.items():
        index={x['uuid']:x for x in csv_rows(inv['output'])}
        r=latest[host]
        for x in csv_rows(r['output']):
            uuid=x['uuid'];reg=registered[uuid];old=index[uuid];used=ledger.get(uuid,dict(memory=0,cores=0,count=0,pods=[]))
            ambiguous=any(p.get('node')==reg['node'] for p in unresolved)
            row=dict(node=reg['node'],index=int(old['index']),uuid=uuid,model=old['name'],physical_total_mib=int(x['memory.total [MiB]']),
                     physical_used_mib=int(x['memory.used [MiB]']),physical_free_mib=int(x['memory.free [MiB]']),
                     driver_reserved_mib=int(x['memory.reserved [MiB]']),utilization_gpu_percent=int(x['utilization.gpu [%]']),
                     physical_sample_utc=r['utc'],registered_capacity_mib=reg['registered_memory'],
                     ledger_used_memory_reconstructed=None if ambiguous else used['memory'],
                     ledger_used_compute_reconstructed=None if ambiguous else used['cores'],
                     allocated_instances_reconstructed=None if ambiguous else used['count'],
                     ledger_free_memory_reconstructed=None if ambiguous else reg['registered_memory']-used['memory'],
                     active_pods=used['pods'],max_instances=reg['max_instances'],health=reg['health'],sharing_mode=reg['mode'],
                     ledger_evidence='API annotations + scheduler source reconstruction; NOT direct scheduler memory/metric read',
                     ledger_api_sample_utc=mgmt['utc'],direct_scheduler_metric_memory=None)
            table.append(row)
    table.sort(key=lambda x:(x['node'],x['index']))
    processes=[]
    for r in records:
        if r['kind']!='gpu_compute_processes':continue
        # Prefer the later complete collection for each host.
        if r is not [z for z in records if z['kind']=='gpu_compute_processes' and z['host']==r['host']][-1]:continue
        for p in csv_rows(r['output']):
            pid=int(p['pid']);ownership={}
            for z in records:
                if z['kind']=='process_ownership' and z.get('pid')==pid and z['host']==r['host']:
                    ownership[Path(z['path']).name]=z.get('output')
            processes.append(dict(host=r['host'].lower(),uuid=p['gpu_uuid'],pid=pid,process_name=p['process_name'],used_memory_mib=p['used_gpu_memory [MiB]'],ownership=ownership))
    # Extract sources with exact newline bytes so hashes can be compared to downloads.
    source_rows=[]
    for r in records:
        if r['kind'] not in ('custom_source','dqn_service_source') or r.get('exit_code')!=0:continue
        sourcepath=r['command'][-1]
        rel=sourcepath.split('/volcano-v1.13.1-dqn/')[-1] if r['kind']=='custom_source' else 'DQN2/grpc/dqn_scheduler_server.py'
        f=root/'deployed-source'/rel;f.parent.mkdir(parents=True,exist_ok=True);data=r['output'].encode('utf-8');f.write_bytes(data)
    for f in (root/'deployed-source').rglob('*'):
        if not f.is_file():continue
        rel=f.relative_to(root/'deployed-source');official=root/'sources/volcano'/rel
        row=dict(file=str(f.relative_to(root)),sha256=hashlib.sha256(f.read_bytes()).hexdigest(),
                 official_file=str(official.relative_to(root)) if official.is_file() else None,
                 matches_official_v1_13_1=official.is_file() and f.read_bytes()==official.read_bytes())
        source_rows.append(row)
    result=dict(data_type='measured_readonly_snapshot + source-based ledger reconstruction',
                creates_pods=False,executes_cuda=False,retrains_models=False,comparison=table,
                gpu_processes=processes,pods=pods,unresolved_active_pods=unresolved,
                direct_scheduler_metrics_available=False,
                source_comparison=source_rows,excluded_node='r5300 (A40)',
                notes=['Measurements are sequential, not an atomic cluster snapshot.',
                       'All logical values are reconstructed from nonterminal Pod annotations, not directly read scheduler internal state.',
                       'Do not sum stale monitor vGPU limits for completed Pods as active allocations.',
                       'NVML v1 HostGPUMemoryUsage and nvidia-smi memory.used can have different reserved-memory accounting.'])
    (out/'audit-summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    with (out/'gpu-logical-physical.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(table[0]));w.writeheader();w.writerows(table)
    header='| 节点/卡号 | 型号 | UUID | 物理总量 MiB | 实际已用 MiB | 实际空闲 MiB | 利用率 % | 账面已分配* MiB / 核配额 / 实例数 | 账面剩余* MiB |'
    lines=[header,'|---|---|---|---:|---:|---:|---:|---|---:|']
    for r in table:
        lines.append('| '+ ' | '.join(str(x) for x in [r['node']+'/'+str(r['index']),r['model'],r['uuid'],r['physical_total_mib'],r['physical_used_mib'],r['physical_free_mib'],r['utilization_gpu_percent'],str(r['ledger_used_memory_reconstructed'])+' / '+str(r['ledger_used_compute_reconstructed'])+' / '+str(r['allocated_instances_reconstructed']),r['ledger_free_memory_reconstructed']])+' |')
    (out/'gpu-comparison-table.md').write_text('\n'.join(lines)+'\n\n*由当前非终态 Pod 注解及当前源码重建；未直接读取 scheduler 内部账本。\n',encoding='utf-8')
    evidence_manifest=[dict(file=str(f.relative_to(root)),bytes=f.stat().st_size,sha256=hashlib.sha256(f.read_bytes()).hexdigest())
                       for f in root.rglob('*') if f.is_file() and f.suffix in ('.jsonl','.json','.txt','.go','.c','.h','.py','.csv','.xml','.md') and f.name!='evidence-manifest.json']
    (out/'evidence-manifest.json').write_text(json.dumps(evidence_manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(dict(gpus=len(table),compute_processes=len(processes),unresolved_active_pods=len(unresolved),output=str(out)),ensure_ascii=False))


if __name__=='__main__':main()
