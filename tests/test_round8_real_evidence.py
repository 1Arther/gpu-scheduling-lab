"""Read preserved real evidence only. No cluster calls, model fits or mock labels."""
import csv
import json
from pathlib import Path

from gpu_lab.staging import native_commit_receipt

OUT=Path(__file__).resolve().parents[1]/'results/round8-live-20261020'
SOURCE=OUT/'evidence/attempt2'


def load(path):return json.loads(path.read_text(encoding='utf-8'))


def table(name):
    with (OUT/'verified'/name).open(encoding='utf-8-sig') as f:return list(csv.DictReader(f))


def test_real_states_are_actual_RF_responses_not_relabelled():
    state=load(SOURCE/'execution/state.json')
    expected=['balanced','training_preferred','unavailable','inference_preferred','balanced']
    assert len(state['states'])==5
    for i,s in enumerate(state['states']):
        c=s['classification']['classification']
        assert c['label']==c['raw_rf']['label']==expected[i]
        assert c['prediction_status']=='provisional'
        assert s['rf_uuid']==s['allocate_uuid']==s['plugin_uuid']==state['target_uuid']
        assert s['gpu_uuid']=='GPU-fb6aaeaf-9ca4-3c9a-7b3b-f56714c1f7cd'


def test_real_eight_UUID_receipts_and_sleeps():
    receipts=list((SOURCE/'execution').glob('*-uuid-proof.json'))
    assert len(receipts)==8
    for p in receipts:
        r=load(p);native=native_commit_receipt(r['native_commit_log'])
        assert r['rf_recommended_uuids']==r['native_permanent_uuids']==r['device_plugin_consumed_uuids']
        assert not native['fallback_used']
        assert r['rf_advice']['model_version']=='rf-v4-known-states-ee35cfabe30f'
        assert r['rf_advice']['request_id']==native['requestID']
        pod=load(p.with_name(p.name.replace('-uuid-proof.json','-pod.json')))
        assert pod['metadata']['uid']==r['pod_uid']
        assert pod['spec']['containers'][0]['command']==['/bin/sleep','600']


def test_native_memory_core_instance_changes_and_no_overallocation():
    for r in table('native-ledger-transitions.csv'):
        assert int(r['native_memory_after'])<=24564
        assert 0<=int(r['native_cores_after'])<=100
        assert 0<=int(r['native_instances_after'])<=10
    states=table('states.csv')
    assert [(int(r['free_memory_mib']),int(r['free_compute_quota']),int(r['allocated_vgpu_count'])) for r in states]==[
        (24564,100,0),(23564,40,1),(6000,35,2),(7000,95,1),(24564,100,0)]


def test_all_eight_releases_and_final_logical_zero():
    assert len(table('release-checks.csv'))==8
    final=load(SOURCE/'execution/cleanup-logical-zero-ledger.json')
    assert not final['node_lock']
    assert all(g['allocated_memory_mib']==g['allocated_compute_quota']==g['allocated_vgpu_count']==0 for g in final['devices'])
    independent=load(SOURCE/'final-independent-readonly.json')
    assert not independent['own_pods'] and not independent['own_podgroups'] and not independent['rf_pods']
    assert all(str(v)=='0' for v in independent['quota_used'].values())


def test_restore_config_model_and_binary_in_both_attempts():
    for name in ('attempt1','attempt2'):
        r=load(OUT/'evidence'/name/'execution/restore-confirmation.json')
        assert r['original_replicas']==r['original_available']==1
        assert r['quota_restored'] and r['rf_replicas']==0 and r['model_and_binary_hashes_equal']
        assert r['original_config_equal'] and not r['cleanup_errors']
        h=r['hashes_after']
        assert h['model_bundle']['random_forest.joblib']=='ee35cfabe30fffc28ff0be09d3738587d4f9bffbe26386e2a9fae5c5b9e80748'
        assert h['original_executable']=='9f2e1ae3359ce234fd048bc48aab082add99a686631808908b5ae518bdebf4f4'


def test_failed_first_attempt_is_preserved_as_failure():
    s=load(OUT/'evidence/attempt1/execution/state.json')
    assert s['experiment_success'] is False and s['restored'] is True
    assert "KeyError" in s['error'] and len(s['pods'])==2
