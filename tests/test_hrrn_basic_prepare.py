import copy
import importlib.util
import json
from pathlib import Path
import sys
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
import prepare_hrrn_basic as basic

def test_only_ordering_changes_between_modes():
    configs = basic.configurations()
    a, b = copy.deepcopy(configs['A']), copy.deepcopy(configs['B'])
    for mode, cfg in configs.items():
        basic.validate_config(cfg, mode)
        args = cfg['tiers'][1]['plugins'][-1]['arguments']
        assert args['complete-job'] is False
        assert args['coordinated-complete-job'] is False
        assert args['reservation-barrier'] is False
    b['tiers'][1]['plugins'][-1]['arguments']['enabled'] = False
    assert a == b

@pytest.mark.parametrize('flag',['complete-job','coordinated-complete-job','reservation-barrier'])
def test_guarded_configs_cannot_be_relabelled_basic(flag):
    cfg = basic.configurations()['B']
    cfg['tiers'][1]['plugins'][-1]['arguments'][flag] = True
    with pytest.raises(ValueError): basic.validate_config(cfg,'B')

@pytest.mark.parametrize('node',list(basic.NODES))
def test_node_artifacts_are_single_pod_and_never_started(tmp_path,node):
    out=tmp_path/node
    basic.render(node,out,'docker.io/gpu-lab/volcano-hrrn@sha256:'+'c'*64)
    protocol=json.loads((out/'protocol.json').read_text(encoding='utf-8'))
    assert protocol['execution_enabled'] is False and protocol['multi_pod_enabled'] is False
    for dep in out.glob('deployment-*.json'):
        obj=json.loads(dep.read_text(encoding='utf-8'))
        assert obj['spec']['replicas']==0
        assert obj['spec']['template']['spec']['nodeSelector']['kubernetes.io/hostname']==node
    for f in out.glob('jobs/*/*.json'):
        objects=json.loads(f.read_text(encoding='utf-8'))['items']
        pods=[o for o in objects if o['kind']=='Pod']
        assert len(pods)==1
        assert pods[0]['spec']['schedulerName']=='volcano-hrrn-basic'
        assert pods[0]['spec']['nodeSelector']['kubernetes.io/hostname']==node
        assert objects[0]['metadata']['annotations']['gpu-lab/total-pods']=='1'
    blockers=list((out/'jobs/order').glob('blocker-*.json'))
    assert len(blockers)==basic.NODES[node][2]-1
