"""Offline collector verification using explicitly synthetic timestamps."""
import importlib.util
from pathlib import Path

spec=importlib.util.spec_from_file_location('stage4_collector',Path(__file__).resolve().parents[1]/'scripts/collect_hrrn_timing.py')
collector=importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)

def fixture():
    return {'items':[{'metadata':{'namespace':'lab','name':'p','uid':'uid','creationTimestamp':'2026-01-01T00:00:00Z',
        'annotations':{'scheduling.k8s.io/group-name':'pg','volcano.sh/vgpu-ids-new':'GPU-1,NVIDIA,4096,20:'}},
        'spec':{'containers':[{'name':'sleep'}]},'status':{'phase':'Succeeded','conditions':[{'type':'PodScheduled','status':'True','lastTransitionTime':'2026-01-01T00:00:07Z'}],
        'containerStatuses':[{'state':{'terminated':{'startedAt':'2026-01-01T00:00:09Z','finishedAt':'2026-01-01T00:00:14Z'}}}]}}]}

def test_separate_observed_intervals_and_missing_decision():
    log='2026-01-01T00:00:02Z HRRN session snapshot job="lab/pg" ready=true readyAt="2026-01-01T00:00:02Z"\n2026-01-01T00:00:15Z rf-job logical release pod="lab/p"'
    r=collector.extract(fixture(),log)[0]
    assert r['queue_wait_seconds']==5
    assert r['pod_start_delay_seconds']==2
    assert r['container_runtime_seconds']==5
    assert r['release_delay_seconds']==1
    assert r['scheduling_decision_seconds'] is None
    assert r['gpu_uuids'].startswith('GPU-1')

def test_native_missing_readiness_is_never_pod_birth():
    r=collector.extract(fixture(),'')[0]
    assert r['ready_at'] is None and r['queue_wait_seconds'] is None
    r=collector.extract(fixture(),'',{'lab/p':0.02},{'lab/pg':collector.timestamp('2026-01-01T00:00:03Z')})[0]
    assert r['queue_wait_seconds']==4 and r['scheduling_decision_seconds']==0.02
    assert r['ready_source']=='common_external_observation'

def test_pending_pod_is_not_completed():
    data=fixture();data['items'][0]['status']={'phase':'Pending'}
    r=collector.extract(data,'')[0]
    assert r['completed_at'] is None and r['container_runtime_seconds'] is None
