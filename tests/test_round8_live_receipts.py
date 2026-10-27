import importlib.util
from pathlib import Path


spec=importlib.util.spec_from_file_location('round8_rules',Path(__file__).resolve().parents[1]/'scripts/round8_receipt_rules.py')
rules=importlib.util.module_from_spec(spec);spec.loader.exec_module(rules)


def device(uuid,mem=0,cores=0,count=0):
    return dict(uuid=uuid,registered_memory_mib=24564,allocated_memory_mib=mem,allocated_compute_quota=cores,
                allocated_vgpu_count=count,max_instances=10,healthy=True)


def test_RF_receipt_is_native_filtered_subset_not_all_registered_UUIDs():
    devices=[device('blocked',64,100,1),device('other')]
    assert rules.candidate_uuids(devices,64,100)=={'other'}
    assert rules.candidate_uuids(devices,1000,60)=={'other'}


def test_full_core_request_cannot_share_existing_low_core_instance():
    assert rules.candidate_uuids([device('g',64,1,1)],64,100)==set()


def test_resources_are_not_overridden_by_classification():
    assert rules.candidate_uuids([device('g',18564,65,2)],17564,5)==set()
    assert rules.candidate_uuids([device('g',18564,65,2)],64,1)=={'g'}


def test_no_cores_request_cannot_share_full_core_device():
    assert rules.candidate_uuids([device('g',64,100,1)],64,0)==set()


def test_API_canonical_quantity_is_resource_units_not_bytes():
    s=importlib.util.spec_from_file_location('round8_analysis',Path(__file__).resolve().parents[1]/'scripts/round8_analyze_live.py')
    m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
    assert m.resource_integer('1k')==m.resource_integer('1000')==1000
    assert m.resource_integer('17564')==17564
