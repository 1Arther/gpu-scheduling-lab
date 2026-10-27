"""Independent EASY forecast/calendar audit on top of the integer ledger audit."""
from ..queue_sim.audit import verify_result
from ..queue_sim.placement import capacity


def verify_study(result,jobs,fleet,nodes=None):
    audit=verify_result(result,jobs,fleet,nodes)
    by_id={j.job_id:j for j in jobs};running={};forecasts={};checked=updates=0
    for e in result['events']:
        jid=e['job_id'];now=e['time'];kind=e['kind']
        if kind=='start':
            j=by_id[jid]
            running[jid]=list(e['plan']['gpu_ids'])
            forecasts[jid]=dict(job_id=jid,gpu_ids=running[jid],start=now,
                expected_end=now+max(j.estimated_service,result['config']['tau']),
                estimated_service=j.estimated_service,extensions=0)
        elif kind in ('complete','cancel'):
            running.pop(jid,None);forecasts.pop(jid,None)
        elif kind=='estimate_update':
            old=forecasts[jid];assert e['previous']==old and now>=old['expected_end']
            expected=now+max(old['estimated_service'],now-old['start'],result['config']['tau'])
            assert e['forecast']['expected_end']==expected
            assert e['forecast']['extensions']==old['extensions']+1
            forecasts[jid]=e['forecast'];updates+=1
        elif kind in ('easy_reserve','easy_reservation_update','easy_backfill_check'):
            assert sorted(e['forecasts'],key=lambda x:x['job_id'])==sorted(forecasts.values(),key=lambda x:x['job_id'])
            r=e.get('reservation')
            if r is None:continue
            owner=by_id[r['job_id']];t=r['expected_start'];assert t>=now-1e-9
            ids=list(r['targets']);assert len(ids)==owner.gpu_count and len(set(ids))==len(ids)
            assert len({e['state'][k]['node'] for k in ids})==1
            assert all(e['state'][k]['online'] for k in ids)
            assert owner.node is None or e['state'][ids[0]]['node']==owner.node
            assert all(not owner.allowed_profiles or fleet[k].profile.profile_id in owner.allowed_profiles for k in ids)
            remaining={k:list(capacity(g)) for k,g in fleet.items()}
            for other,targets in running.items():
                if forecasts[other]['expected_end']>t:
                    q=by_id[other].request
                    for k in targets:
                        remaining[k]=[a-b for a,b in zip(remaining[k],(q.memory_mib,q.compute_units,q.instances))]
            if kind=='easy_backfill_check':
                j=by_id[jid]
                assert e['candidate_expected_end']==now+max(j.estimated_service,result['config']['tau'])
                if e['candidate_expected_end']>t:
                    for k in e['gpu_ids']:
                        remaining[k]=[a-b for a,b in zip(remaining[k],(j.request.memory_mib,j.request.compute_units,j.request.instances))]
                checked+=1
            for k in ids:
                assert all(a<=b for a,b in zip((owner.request.memory_mib,owner.request.compute_units,owner.request.instances),remaining[k]))
                if owner.request.compute_units==100:assert remaining[k][2]==fleet[k].max_instances
    return dict(**audit,easy_backfills_checked=checked,estimate_extensions_checked=updates)
