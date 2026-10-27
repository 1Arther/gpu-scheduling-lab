"""Independent integer/event replay: no Simulator or Cluster commit is called."""
from .placement import capacity
from .metrics import NAMES


def verify_result(result, jobs, fleet, nodes=None):
    by_id = {j.job_id:j for j in jobs}
    nodes = dict(nodes) if nodes is not None else {k:k.rsplit('/gpu-',1)[0] for k in fleet}
    caps = {k:capacity(g) for k,g in fleet.items()}; ledger = {k:{} for k in fleet}
    revisions = dict.fromkeys(fleet,0); online = dict.fromkeys(fleet,True)
    status = {}; starts = {}; assignments = {}; active = None; epochs=set()
    last = 0.; areas_a=[0.,0.,0.]; areas_p=[0.,0.,0.]
    checked=atomic=guarded=releases=0; window=result['config']['observation_end']

    def remaining():
        return {k:tuple(caps[k][d]-sum(v[d] for v in allocations.values()) for d in range(3)) for k,allocations in ledger.items()}

    for e in result['events']:
        now=e['time']; assert now>=last
        before=remaining(); dt=max(0.,min(now,window)-min(last,window))
        for d in range(3):
            areas_a[d]+=sum(caps[k][d]-before[k][d] for k in fleet)*dt
            areas_p[d]+=(sum(min(before[k][d],r[d]) for k,r in active['required'].items()) if active else 0.)*dt
        kind=e['kind']; jid=e['job_id']
        if kind in ('arrival','ready'):
            assert now==(by_id[jid].arrival_at if kind=='arrival' else by_id[jid].ready_at)
            status[jid]='not_ready' if kind=='arrival' else 'waiting'
        elif kind=='reject': status[jid]='rejected'
        elif kind=='availability': online[e['gpu_id']]=e['online']
        elif kind=='protection_create':
            assert active is None
            active=e['protection']; j=by_id[jid]; ids=active['targets']
            assert active['job_id']==jid and status[jid]=='waiting' and len(ids)==j.gpu_count and len(set(ids))==len(ids)
            assert len({nodes[k] for k in ids})==1 and all(online[k] for k in ids)
            assert active['epoch'] not in epochs; epochs.add(active['epoch'])
            for k in ids:
                need=(j.request.memory_mib,j.request.compute_units,j.request.instances)
                assert all(a<=b for a,b in zip(need,caps[k]))
                expected=caps[k] if active['mode']=='whole_gpu' else need
                if j.request.compute_units==100: expected=(expected[0],expected[1],caps[k][2])
                assert list(expected)==active['required'][k]
        elif kind=='protection_clear':
            assert active==e['previous']; active=None
        elif kind=='start':
            j=by_id[jid]; ids=list(e['plan']['gpu_ids'])
            assert e['plan']['protection_epoch']==(active['epoch'] if active else None)
            assert status[jid]=='waiting' and now>=j.ready_at
            assert len(ids)==j.gpu_count and len(set(ids))==len(ids) and len({nodes[k] for k in ids})==1
            assert j.node is None or nodes[ids[0]]==j.node
            need=(j.request.memory_mib,j.request.compute_units,j.request.instances)
            for k in ids:
                assert online[k] and (not j.allowed_profiles or fleet[k].profile.profile_id in j.allowed_profiles)
                assert all(a<=b for a,b in zip(need,before[k]))
                if j.request.compute_units==100: assert not ledger[k]
                assert jid not in ledger[k]
            if active is not None:
                if active['job_id']==jid: assert ids==active['targets']
                else:
                    for k in ids:
                        if k in active['required']:
                            assert all(a<=max(0,f-r) for a,f,r in zip(need,before[k],active['required'][k]))
                    guarded+=1
            for k in ids: ledger[k][jid]=need; revisions[k]+=1
            starts[jid]=now; assignments[jid]=ids; status[jid]='running'; atomic+=1
            after=remaining()
            if active is not None and active['job_id']!=jid:
                for k,r in active['required'].items(): assert all(min(a,t)>=min(b,t) for a,b,t in zip(after[k],before[k],r))
        elif kind in ('complete','cancel'):
            if kind=='complete':
                assert status[jid]=='running' and abs(now-starts[jid]-by_id[jid].actual_service)<1e-8
            if status.get(jid)=='running':
                for k in assignments[jid]: del ledger[k][jid]; revisions[k]+=1
                releases+=1
            status[jid]='completed' if kind=='complete' else 'cancelled'
        assert e['protection']==active
        remaining_now=remaining()
        assert set(e['state'])==set(fleet)
        for k,g in e['state'].items():
            assert tuple((g['remaining_memory_mib'],g['remaining_compute_units'],g['remaining_instances']))==remaining_now[k]
            assert all(0<=x<=c for x,c in zip(remaining_now[k],caps[k]))
            assert g['revision']==revisions[k] and g['online']==online[k] and g['node']==nodes[k]
            assert {a['allocation_id'] for a in g['allocations']}==set(ledger[k])
        last=now; checked+=1
    assert active is None and all(not v for v in ledger.values())
    for row in result['jobs']:
        assert not row['allocation_active']
        if row['start_at'] is not None:
            expected=row['finish_at'] if row['status']=='completed' else row['cancelled_at']
            assert row['released_at']==expected
        else:
            assert row['released_at'] is None
    for d,n in enumerate(NAMES):
        assert abs(areas_a[d]-result['metrics']['allocated_capacity_time'][n])<1e-6
        assert abs(areas_p[d]-result['metrics']['protected_idle_capacity_time'][n])<1e-6
    return dict(success=True, events_checked=checked, atomic_starts=atomic, guarded_starts=guarded,
        releases=releases, protection_epochs=len(epochs), resource_time_dimensions_checked=3,
        all_resources_released=True, synthetic_durations_only=True)
