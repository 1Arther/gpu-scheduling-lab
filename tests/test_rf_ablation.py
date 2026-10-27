"""Frozen-model single-term ablation, not new fitting or accuracy evaluation."""
from copy import deepcopy
import pytest
from gpu_lab.runtime.service import SuitabilityService
from gpu_lab.runtime.volcano import VolcanoRFAdapter
from test_round6 import payload


@pytest.fixture(scope='module')
def svc():return SuitabilityService()


@pytest.mark.parametrize('task',['training','inference'])
def test_pair_changes_only_learned_term(svc,task):
    adapter=VolcanoRFAdapter(svc);p=payload(svc,task)
    on=adapter.rank(p);p['rf_prediction_enabled']=False;off=adapter.rank(p)
    assert on['rf_prediction_enabled'] is True and off['rf_prediction_enabled'] is False
    for a,b in zip(on['classifications'],off['classifications']):
        assert a['snapshot']==b['snapshot'] and a['classification']==b['classification']
        x,y=a['request_specific_scores'][task],b['request_specific_scores'][task]
        dx,dy=x['score_decomposition'],y['score_decomposition']
        assert dx['heuristic_contribution']==pytest.approx(dy['heuristic_contribution'])
        assert x['score']-y['score']==pytest.approx(dx['applied_rf_contribution'])
        assert dy['applied_rf_contribution']==0
        if a['classification']['prediction_status']=='pending_verification':
            assert dx['applied_rf_contribution']==0
            assert x['score']==y['score']
            assert dx['conservative_fallback_contribution']==dy['conservative_fallback_contribution']


def test_ablation_never_changes_admission(svc):
    adapter=VolcanoRFAdapter(svc);p=payload(svc)
    p['gpus'][0]['allocated_memory_mib']=24000
    before=deepcopy(p);a=adapter.rank(p);p['rf_prediction_enabled']=False;b=adapter.rank(p)
    assert [r['gpu_uuid'] for r in a['details']['rejected_gpus']]==[r['gpu_uuid'] for r in b['details']['rejected_gpus']]
    assert a['details']['rejected_gpus'][0]['rejection_reasons']==b['details']['rejected_gpus'][0]['rejection_reasons']
    assert before['gpus']==p['gpus']


def test_switch_must_be_boolean(svc):
    p=payload(svc);p['rf_prediction_enabled']='false'
    assert VolcanoRFAdapter(svc).rank(p)['status']=='invalid_input'
