"""Offline evidence parsing checks. These tests do not call cluster APIs or train RF."""
import importlib.util
import json
from pathlib import Path
import pytest
import yaml

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('audit_replay',ROOT/'scripts/summarize_vgpu_audit.py')
mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)


def test_terminal_pods_not_counted_in_active_ledger():
    p=dict(namespace='default',name='completed',phase='Succeeded',annotations={'volcano.sh/vgpu-ids-new':'GPU-1,NVIDIA,8192,10:'})
    assert mod.reconstruct_ledger([p])==({},[])


def test_uuid_memory_core_instance_annotation_parsing():
    p=dict(namespace='default',name='active',phase='Running',annotations={'volcano.sh/vgpu-ids-new':'GPU-a,NVIDIA,8192,10:GPU-b,NVIDIA,8192,10:;GPU-a,NVIDIA,1024,5:'})
    ledger,unresolved=mod.reconstruct_ledger([p])
    assert not unresolved
    assert (ledger['GPU-a']['memory'],ledger['GPU-a']['cores'],ledger['GPU-a']['count'])==(9216,15,2)
    assert ledger['GPU-b']['memory']==8192


def test_active_unresolved_request_is_reported_not_fabricated_zero():
    p=dict(namespace='default',name='pending',phase='Pending',node='test',containers=[dict(resources=dict(limits={'volcano.sh/vgpu-number':'1'}))])
    assert mod.reconstruct_ledger([p])[1][0]['name']=='pending'


def test_invalid_device_annotation_is_not_silently_accepted():
    with pytest.raises(ValueError):mod.allocated_devices('GPU-1,NVIDIA,8192:')


def test_physical_csv_preserves_reported_free_not_total_minus_used():
    row=mod.csv_rows('uuid, memory.total [MiB], memory.used [MiB], memory.free [MiB]\nGPU-1, 32607, 31044, 1107\n')[0]
    assert int(row['memory.free [MiB]'])==1107
    assert int(row['memory.total [MiB]'])-int(row['memory.used [MiB]'])!=1107


def test_pending_yaml_does_not_bypass_scheduler_or_execute_cuda():
    p=yaml.safe_load((ROOT/'k8s/audit/vgpu-quota-no-cuda.pending-approval.yaml').read_text(encoding='utf-8'))
    assert p['spec']['schedulerName']=='volcano'
    assert 'nodeName' not in p['spec']
    c=p['spec']['containers'][0]
    assert c['args']==['exec sleep 300']
    assert c['resources']['limits']==c['resources']['requests']
    assert c['resources']['limits']['volcano.sh/vgpu-memory']==8192
    assert c['imagePullPolicy']=='Never'


def test_replayed_snapshot_marks_ledger_inference_explicitly():
    p=ROOT/'results/vgpu-mechanism-audit-20261019/summary/audit-summary.json'
    if not p.is_file():pytest.skip('Optional captured cluster evidence not present')
    data=json.loads(p.read_text(encoding='utf-8'))
    assert len(data['comparison'])==11
    assert len({r['uuid'] for r in data['comparison']})==11
    assert all(r['direct_scheduler_metric_memory'] is None for r in data['comparison'])
    assert not data['creates_pods'] and not data['executes_cuda'] and not data['retrains_models']
