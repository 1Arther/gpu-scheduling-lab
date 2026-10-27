"""Read-only stage-7 evidence helpers. No Kubernetes client or resource owner."""
from collections import defaultdict
import csv
import io
import json
import math
import re

PERMANENT_IDS='volcano.sh/vgpu-ids-new'
TRANSIENT_IDS='volcano.sh/devices-to-allocate'


def native_commit_receipt(line):
    """Read the real Go InfoS contract: Fallback is a reason string, not bool."""
    if 'rf-job UUID allocation' not in line:raise ValueError('not a native UUID commit log')
    out={}
    for field in ('pod','podUID','node','requestID','devices','fallback'):
        match=re.search(r'\b'+field+r'=("(?:\\.|[^"\\])*")',line)
        if not match:raise ValueError('missing or invalid native commit field: '+field)
        out[field]=json.loads(match.group(1))
    out['allocations']=allocations(out['devices'])
    out['fallback_used']=bool(out['fallback'])
    out.update(native_ledger_snapshots(line))
    return out


def native_ledger_snapshots(line):
    """InfoS serializes the Go GPU structs as JSON arrays, independently of RF."""
    result={}
    for key in ('logicalBefore','logicalAfter'):
        match=re.search(r'\b'+key+r'=',line)
        if match:
            value,_=json.JSONDecoder().raw_decode(line[match.end():])
            if not isinstance(value,list):raise ValueError('native ledger snapshot must be a list')
            result[key]=value
    return result


def allocations(text):
    result=[]
    for ci,container in enumerate((text or '').split(';')):
        for unit in container.split(':'):
            if not unit:continue
            parts=unit.split(',')
            if len(parts)!=4:raise ValueError('malformed allocation annotation')
            uuid,kind,memory,cores=parts;memory,cores=int(memory),int(cores)
            if not uuid or memory<0 or not 0<=cores<=100:raise ValueError('invalid allocation')
            result.append({'uuid':uuid,'type':kind,'memory_mib':memory,'compute_quota':cores,'container_index':ci})
    return result


def inventory(nodes,pods):
    devices={};warnings=[]
    for node in nodes:
        name=node['metadata']['name']
        for unit in node['metadata']['annotations'].get('volcano.sh/node-vgpu-register','').split(':'):
            if not unit:continue
            fields=unit.split(',')
            if len(fields)!=6:raise ValueError('unsupported registered GPU format')
            uuid,count,memory,model,healthy,mode=fields
            if uuid in devices:raise ValueError('duplicate registered physical UUID')
            devices[uuid]={'uuid':uuid,'node':name,'model':model,'registered_memory_mib':int(memory),'max_instances':int(count),
                           'healthy':healthy=='true','sharing_mode':mode,'allocated_memory_mib':0,'allocated_compute_quota':0,
                           'allocated_vgpu_count':0,'training_vgpu_count':0,'inference_vgpu_count':0,
                           'training_pod_count':0,'inference_pod_count':0,'unrestricted_vgpu_count':0,'pods':[],
                           'source':'reconstructed_from_active_Pod_annotations_not_scheduler_memory',
                           'node_ready':any(c['type']=='Ready' and c['status']=='True' for c in node['status']['conditions'])}
    for pod in pods:
        if pod['status'].get('phase') in ('Succeeded','Failed'):continue
        m,s=pod['metadata'],pod['spec'];ann=m.get('annotations') or {};label=(m.get('labels') or {}).get('gpu-lab/workload-type')
        requests=[(c.get('resources') or {}).get(field,{}) for c in s.get('containers',[])+s.get('initContainers',[])
                  for field in ('limits','requests')]
        has_gpu=any(any('gpu' in k for k in r) for r in requests)
        text=ann.get(PERMANENT_IDS) or ann.get(TRANSIENT_IDS)
        if has_gpu and not text:
            warnings.append({'pod':m['namespace']+'/'+m['name'],'uid':m['uid'],'node':s.get('nodeName'),
                             'reason':'GPU request has no per-UUID quota annotation; ordinary GPU may overlap vGPU capacity'})
        touched=set()
        for row in allocations(text):
            if row['uuid'] not in devices:
                warnings.append({'pod':m['name'],'uuid':row['uuid'],'reason':'annotation UUID not registered'});continue
            g=devices[row['uuid']];g['allocated_memory_mib']+=row['memory_mib'];g['allocated_compute_quota']+=row['compute_quota'];g['allocated_vgpu_count']+=1
            if label in ('training','inference'):g[label+'_vgpu_count']+=1
            if row['compute_quota']==0:g['unrestricted_vgpu_count']+=1
            g['pods'].append({'namespace':m['namespace'],'name':m['name'],'uid':m['uid'],'workload_type':label,
                              'phase':pod['status'].get('phase'),'terminating':bool(m.get('deletionTimestamp')),
                              'allocation_annotation':PERMANENT_IDS if ann.get(PERMANENT_IDS) else TRANSIENT_IDS,**row})
            touched.add(g['uuid'])
        for uuid in touched:
            if label in ('training','inference'):devices[uuid][label+'_pod_count']+=1
    for g in devices.values():
        g['free_memory_mib']=g['registered_memory_mib']-g['allocated_memory_mib'];g['free_compute_quota']=100-g['allocated_compute_quota']
        g['free_instances']=g['max_instances']-g['allocated_vgpu_count']
        if min(g['free_memory_mib'],g['free_compute_quota'],g['free_instances'])<0:warnings.append({'uuid':g['uuid'],'reason':'annotation reconstruction indicates over-allocation'})
    return list(devices.values()),warnings


def physical_rows(text):
    rows=[]
    for row in csv.reader(io.StringIO(text)):
        if not row:continue
        if len(row)!=8:raise ValueError('unexpected nvidia-smi query format')
        index,uuid,model,total,used,free,util,driver=(v.strip() for v in row)
        rows.append({'index':int(index),'uuid':uuid,'physical_model':model,'physical_total_mib':int(total),
                     'physical_used_mib':int(used),'physical_free_mib':int(free),'physical_utilization_pct':int(util),
                     'driver':driver,'physical_state_source':'direct nvidia-smi; NOT RF dynamic quota input'})
    return rows


def fragmentation(gpus,memory_mib=4096,compute_quota=20):
    if memory_mib<=0 or not 0<compute_quota<=100:raise ValueError('invalid reference request')
    free=[(max(0,g['free_memory_mib']),max(0,g['free_compute_quota']),max(0,g['free_instances'])) for g in gpus]
    slots=[min(m//memory_mib,c//compute_quota,n) for m,c,n in free]
    bound=min(sum(m for m,c,n in free)//memory_mib,sum(c for m,c,n in free)//compute_quota,sum(n for m,c,n in free))
    return {'reference_request':{'memory_mib':memory_mib,'compute_quota':compute_quota,'instances':1},
            'feasible_reference_slots':sum(slots),'aggregate_upper_bound_slots':bound,
            'fragmentation_ratio':1-sum(slots)/bound if bound else None,
            'stranded_memory_mib':sum(m for (m,c,n),s in zip(free,slots) if s==0),
            'stranded_compute_quota':sum(c for (m,c,n),s in zip(free,slots) if s==0),
            'scope':'single-vGPU positive-quota logical accounting only; no physical availability or performance claim'}


def uuid_consistency(rf_uuids,permanent_annotation,consumed_uuids,*,consumed_source=None,transient_annotation=None):
    native=[r['uuid'] for r in allocations(permanent_annotation)]
    # Empty devices-to-allocate is normal after plugin consumption, not a mismatch.
    ready=rf_uuids is not None and consumed_uuids is not None and consumed_source in ('kubelet_checkpoint_allocate_response','cri_runtime_NVIDIA_VISIBLE_DEVICES')
    return {'status':'confirmed' if ready and rf_uuids==native==consumed_uuids else 'mismatch' if ready else 'insufficient_evidence',
            'rf_uuids':rf_uuids,'native_annotation_uuids':native,'plugin_consumed_uuids':consumed_uuids,
            'plugin_consumed_source':consumed_source,'devices_to_allocate':transient_annotation,
            'note':'Pod Running, bind-phase=success or empty transient annotation alone is insufficient'}


def logical_rates(gpus):
    total=sum(g['registered_memory_mib'] for g in gpus)
    return {'logical_memory_reservation_rate':sum(g['allocated_memory_mib'] for g in gpus)/total if total else None,
            'logical_compute_reservation_rate':sum(g['allocated_compute_quota'] for g in gpus)/(100*len(gpus)) if gpus else None,
            'source':'annotation reconstruction; excludes untracked host processes and unresolved ordinary-GPU reservations'}
