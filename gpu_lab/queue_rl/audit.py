"""Independent resource audit plus public-action provenance checks."""
from ..queue_study.audit import verify_study

def verify_rl(result,jobs,fleet):
    audit=verify_study(result,jobs,fleet);decisions=0
    expected=None
    for e in result['events']:
        if e['kind']=='rl_decision':
            d=e['decision'];a=d['action'];c=d['context'];decisions+=1
            assert d['mask'][0] and d['mask'][a]
            assert 'actual_service' not in str(c) and 'finish_at' not in str(c)
            assert all(j['ready_at']<=e['time'] for j in c['queue'])
            assert not e['protection']
            for p in c['combos']:
                assert len(p['targets'])==c['owner']['gpu_count'] and len(set(p['targets']))==len(p['targets'])
                assert all(e['state'][k]['online'] for k in p['targets'])
            expected=(c['owner']['job_id'],c['combos'][a-1]['targets']) if a else None
        elif e['kind']=='protection_create':
            assert expected==(e['job_id'],e['protection']['targets']);expected=None
    return dict(**audit,rl_decisions_checked=decisions)
