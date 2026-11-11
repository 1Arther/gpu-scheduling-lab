"""Pure evidence analysis for the frozen native HRRN scheduler.

No cluster client, clock manipulation, or inferred successful API binding from
speculative allocation logs. Callers retain the raw Pod/PG/log/checkpoint inputs.
"""
from datetime import datetime
import json
import math
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gpu_lab.staging import allocations, native_commit_receipt
from round7_collect import checkpoint

MODEL = 'rf-v4-known-states-ee35cfabe30f'
ASSIGNMENT_KEYS = ('volcano.sh/vgpu-node', 'volcano.sh/vgpu-time',
                   'volcano.sh/vgpu-ids-new', 'volcano.sh/devices-to-allocate',
                   'volcano.sh/bind-phase', 'volcano.sh/bind-time')
_FIELD = re.compile(r'\b([A-Za-z][A-Za-z0-9]*)=("(?:\\.|[^"\\])*"|true|false|[-+0-9.eE]+)(?=\s|$)')


def timestamp(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return dt.timestamp() if dt.tzinfo is not None else None
    except (ValueError, TypeError, OverflowError):
        return None


def _fields(line):
    result = {}
    for key, value in _FIELD.findall(line):
        try:
            result[key] = json.loads(value)
        except json.JSONDecodeError:
            continue
    return result


def _line_time(line):
    value = line.split(maxsplit=1)[0] if line.strip() else ''
    return value if timestamp(value) is not None else None


def _nanoseconds(value):
    """Compare the Go RFC3339Nano readyAt values without float rounding."""
    match = re.fullmatch(r'(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d{1,9}))?(Z|[+-]\d\d:\d\d)', value or '')
    if not match:
        return None
    base = timestamp(match[1]+match[3])
    return None if base is None else int(base)*10**9+int((match[2] or '').ljust(9, '0'))


def _items(value):
    return value.get('items', []) if isinstance(value, dict) else (value or [])


def parse_snapshots(log):
    """Return complete HRRN snapshot records in log order; partial tails ignored."""
    rows = []
    for number, line in enumerate(log.splitlines()):
        if 'HRRN session snapshot' not in line:
            continue
        f = _fields(line)
        if not all(k in f for k in ('session', 'job', 'ready', 'readyAt', 'waitingSeconds', 'estimatedSeconds', 'score')):
            continue
        values = [f[k] for k in ('waitingSeconds', 'estimatedSeconds', 'score')]
        if not all(isinstance(x, (int, float)) and math.isfinite(x) for x in values):
            continue
        rows.append(dict(line_number=number, observed_at=_line_time(line), session=f['session'],
            job=f['job'], ready=f['ready'], ready_at=f['readyAt'], waiting_seconds=f['waitingSeconds'],
            estimated_seconds=f['estimatedSeconds'], score=f['score'], reason=f.get('reason'),
            complete_job=f.get('completeJob'), evidence_source='direct_HRRN_session_snapshot', raw_line=line))
    return rows


def latest_waiting(log, job_names):
    """Map requested names to latest snapshots (or None). Prefer namespace/name.

    A basename is accepted only if it identifies exactly one JobInfo in the log.
    A latest not-ready record replaces prior readiness rather than reusing it.
    """
    latest = {r['job']: r for r in parse_snapshots(log)}
    result = {}
    for name in job_names:
        matches = [k for k in latest if k == name or ('/' not in name and k.endswith('/'+name))]
        result[name] = latest[matches[0]] if len(matches) == 1 else None
    return result


def _session_context(log):
    """Use native framework session boundaries, also when HRRN is disabled.

    HRRN snapshots are a fallback for bounded log extracts lacking Open Session.
    Closing a Session clears context so asynchronous release logs are not
    attributed to a completed scheduling cycle.
    """
    snapshots = {s['line_number']: s for s in parse_snapshots(log)}
    contexts, session = {}, None
    for i, line in enumerate(log.splitlines()):
        opened = re.search(r'\bOpen Session (\S+) with <\d+> Job and <\d+> Queues', line)
        if opened:
            session = opened[1]
        if i in snapshots:
            session = snapshots[i]['session']
        contexts[i] = session
        closed = re.search(r'\bClose Session (\S+)', line)
        if closed:
            session = None
    return contexts


def parse_decisions(log):
    """Comparator inputs and selection/planning logs, never a bind-success claim."""
    contexts = _session_context(log)
    patterns = {
        'priority': re.compile(r'Priority JobOrderFn: <([^>]+)> priority: (-?\d+), <([^>]+)> priority: (-?\d+)'),
        'gang': re.compile(r'Gang JobOrderFn: <([^>]+)> is ready: (true|false), <([^>]+)> is ready: (true|false)'),
        'drf': re.compile(r'DRF JobOrderFn: <([^>]+)> share state: ([-+0-9.eE]+), <([^>]+)> share state: ([-+0-9.eE]+)'),
    }
    out = dict(comparisons=[], job_selections=[], task_plans=[], releases=[], rollback_errors=[])
    for i, line in enumerate(log.splitlines()):
        common = dict(line_number=i, observed_at=_line_time(line), session=contexts[i], raw_line=line)
        for plugin, pattern in patterns.items():
            m = pattern.search(line)
            if not m:
                continue
            left, a, right, b = m.groups()
            a, b = ((a == 'true', b == 'true') if plugin == 'gang' else (float(a), float(b)))
            # Same return rules as priority.go, gang.go and drf.go. These are
            # derived returns; the binary directly logs the inputs only.
            result = 0 if a == b else (-1 if ((a > b) if plugin == 'priority' else (a < b)) else 1)
            out['comparisons'].append(dict(common, plugin=plugin, left=left, right=right,
                left_value=a, right_value=b, result=result, tied=result == 0,
                result_source='derived_from_direct_logged_comparator_inputs'))
        m = re.search(r'Try to allocate resource to (\d+) tasks of Job <([^>]+)>', line)
        if m:
            out['job_selections'].append(dict(common, job=m[2], task_count=int(m[1]),
                evidence_source='allocate_action_selection_not_binding'))
        m = re.search(r'Binding Task <([^>]+)> to node <([^>]+)>', line)
        if m:
            out['task_plans'].append(dict(common, pod=m[1], node=m[2],
                evidence_source='speculative_plan_not_API_binding'))
        if 'rf-job logical release' in line:
            f = _fields(line)
            if f.get('podUID'):
                out['releases'].append(dict(common, pod=f.get('pod'), pod_uid=f['podUID'], node=f.get('node')))
        if 'HRRN rollback' in line:
            out['rollback_errors'].append(common)
    return out


def comparison_evidence(log, left, right, session=None, before_line=None):
    """Collect pairwise evidence; HRRN direction remains explicitly derived."""
    snapshots = [r for r in parse_snapshots(log) if before_line is None or r['line_number'] <= before_line]
    if session is None:
        shared = {r['session'] for r in snapshots if r['job'] == left} & {r['session'] for r in snapshots if r['job'] == right}
        session = next((r['session'] for r in reversed(snapshots) if r['session'] in shared), None)
    pair = {r['job']: r for r in snapshots if r['session'] == session and r['job'] in (left, right)}
    direct = [r for r in parse_decisions(log)['comparisons']
              if {r['left'], r['right']} == {left, right} and (session is None or r['session'] == session)
              and (before_line is None or r['line_number'] <= before_line)]
    direction = None
    if len(pair) == 2 and all(_nanoseconds(r['ready_at']) is not None for r in pair.values()):
        a, b = pair[left], pair[right]
        ka, kb = (-a['score'], _nanoseconds(a['ready_at']), left), (-b['score'], _nanoseconds(b['ready_at']), right)
        direction = -1 if ka < kb else 1 if ka > kb else 0
    return dict(session=session, direct_preceding_comparator_inputs=direct,
        all_three_preceding_plugins_have_tie_evidence=all(any(r['plugin'] == p and r['tied'] for r in direct) for p in ('priority', 'gang', 'drf')),
        any_preceding_non_tie=any(not r['tied'] for r in direct), snapshots=pair,
        hrrn_result=direction, hrrn_result_source='derived_from_session_snapshot_and_frozen_compare_source',
        hrrn_direct_comparator_log_available=False,
        hrrn_eligibility_boundary='The candidate direction assumes same queue and priority; verify those from PG/Pod metadata. A prior non-tie can short-circuit HRRN.',
        limitation='Tie evidence within a Session does not identify every individual comparator invocation; verify actual chosen/bound order separately.')


def _containers(pod):
    starts, finishes = [], []
    for c in pod.get('status', {}).get('containerStatuses', []):
        state = c.get('state', {})
        if 'terminated' in state:
            starts.append(state['terminated'].get('startedAt'))
            finishes.append(state['terminated'].get('finishedAt'))
        elif 'running' in state:
            starts.append(state['running'].get('startedAt'))
    starts = [v for v in starts if timestamp(v) is not None]
    finishes = [v for v in finishes if timestamp(v) is not None]
    return (min(starts, key=timestamp) if starts else None,
            max(finishes, key=timestamp) if len(finishes) == len(pod.get('spec', {}).get('containers', [])) and finishes else None)


def _difference(end, start):
    a, b = timestamp(end), timestamp(start)
    return None if a is None or b is None else a-b


def extract_timing(pods, groups, log, observations=None):
    """Return per-Pod rows. observations may contain pods[UID], jobs[ns/PG].

    Per-Pod optional keys: first_bound_at, deleted_at, binding_api_at,
    binding_api_source='audit', decision_started_at, decision_finished_at.
    Per-Job optional key: complete_ready_observed_at (external API observation).
    Neither an external readiness time nor PodScheduled is an internal clock.
    """
    observations = observations or {}
    bygroup = {g['metadata']['namespace']+'/'+g['metadata']['name']: g for g in _items(groups)}
    snapshots, decisions = parse_snapshots(log), parse_decisions(log)
    rows = []
    for p in _items(pods):
        m, sp, st = p['metadata'], p.get('spec', {}), p.get('status', {})
        uid, name = m['uid'], m['namespace']+'/'+m['name']
        group_name = m.get('annotations', {}).get('scheduling.k8s.io/group-name')
        job_info = m['namespace']+'/'+group_name if group_name else None
        group = bygroup.get(job_info, {})
        job_owner = next((o for o in group.get('metadata', {}).get('ownerReferences', [])
                          if o.get('kind') == 'Job' and o.get('apiVersion', '').startswith('batch.volcano.sh/')), None)
        po = observations.get('pods', {}).get(uid, {})
        jo = observations.get('jobs', {}).get(job_info, {})
        scheduled = next((c.get('lastTransitionTime') for c in st.get('conditions', [])
                          if c.get('type') == 'PodScheduled' and c.get('status') == 'True'), None)
        started, completed = _containers(p)
        cutoff = timestamp(scheduled)
        eligible = [s for s in snapshots if s['job'] == job_info and
                    (cutoff is None or (timestamp(s['observed_at']) is not None and timestamp(s['observed_at']) <= cutoff))]
        snap = eligible[-1] if eligible else None
        ready = snap['ready_at'] if snap and snap['ready'] else None
        external_ready = jo.get('complete_ready_observed_at')
        end_trigger = completed or po.get('deleted_at') or m.get('deletionTimestamp')
        release = next((r for r in decisions['releases'] if r['pod_uid'] == uid and timestamp(end_trigger) is not None
                        and timestamp(r['observed_at']) is not None and timestamp(r['observed_at']) >= timestamp(end_trigger)), None)
        audit = po.get('binding_api_at') if po.get('binding_api_source') == 'audit' else None
        row = dict(pod=name, pod_uid=uid, podgroup_uid=group.get('metadata', {}).get('uid'),
            job_info_id=job_info, volcano_job_uid=job_owner.get('uid') if job_owner else None,
            submitted_at=m.get('creationTimestamp'), complete_ready_at=ready,
            complete_ready_source='direct_HRRN_snapshot' if ready else None,
            external_complete_ready_observed_at=external_ready,
            hrrn_snapshot=snap, estimated_service_seconds=snap.get('estimated_seconds') if snap else None,
            waiting_seconds=snap.get('waiting_seconds') if snap else None, hrrn_score=snap.get('score') if snap else None,
            snapshot_selection='last snapshot no later than PodScheduled proxy; exact binding score is not inferred',
            pod_scheduled_at=scheduled, binding_api_at=audit,
            binding_api_evidence_source='audit' if audit else None, first_bound_observed_at=po.get('first_bound_at'),
            pod_started_at=st.get('startTime'), container_started_at=started, completed_at=completed,
            deleted_at=po.get('deleted_at') or m.get('deletionTimestamp'), logical_release_at=release['observed_at'] if release else None,
            queue_wait_seconds=_difference(scheduled, ready), external_queue_wait_seconds=_difference(scheduled, external_ready),
            scheduling_decision_seconds=_difference(po.get('decision_finished_at'), po.get('decision_started_at')),
            container_start_after_pod_scheduled_seconds=_difference(started, scheduled),
            container_runtime_seconds=_difference(completed, started), node=sp.get('nodeName'), phase=st.get('phase'),
            gpu_permanent_annotation=m.get('annotations', {}).get('volcano.sh/vgpu-ids-new'),
            binding_timestamp_boundary='PodScheduled is a status-transition proxy; exact API binding time remains null without audit',
            runtime_boundary='Observed sleep container duration, not a training/inference performance measurement')
        row['negative_time_delta_detected'] = any(row[k] is not None and row[k] < 0 for k in
            ('queue_wait_seconds', 'external_queue_wait_seconds', 'container_start_after_pod_scheduled_seconds', 'container_runtime_seconds'))
        rows.append(row)
    return rows


def rollback_annotations(pods):
    rows = []
    for pod in _items(pods):
        m, sp, st = pod['metadata'], pod.get('spec', {}), pod.get('status', {})
        residual = {k: m.get('annotations', {}).get(k) for k in ASSIGNMENT_KEYS if m.get('annotations', {}).get(k)}
        rows.append(dict(pod=m['namespace']+'/'+m['name'], uid=m['uid'], node=sp.get('nodeName'),
                         phase=st.get('phase'), residual_assignment_annotations=residual,
                         clean=not sp.get('nodeName') and st.get('phase') in (None, 'Pending') and not residual))
    return dict(clean=bool(rows) and all(r['clean'] for r in rows), pods=rows,
                boundary='API metadata only; pair with native release and logical ledger evidence')


def uuid_evidence(pod, scheduler_log, rf_log, checkpoint_json):
    """Correlate the actually bound UID/containers with RF/native/plugin UUIDs.

    Native 'UUID allocation' is speculative. Select the final allocation segment
    after the last earlier native release, and correlate RF by requestID. If that
    segment contains multiple distinct receipts, keep request ambiguity visible.
    """
    m, sp = pod['metadata'], pod.get('spec', {})
    uid, name = m['uid'], m['namespace']+'/'+m['name']
    result = dict(pod=name, pod_uid=uid, status='insufficient_evidence', uuid_consistent=False,
                  selected_request_id=None, native_allocation_is_bind_commit=False, issues=[])
    def issue(message, status='insufficient_evidence'):
        result['issues'].append(message)
        result['status'] = status
        return result
    if not sp.get('nodeName'):
        return issue('Pod is unbound; speculative UUID annotations do not prove a startup')
    try:
        permanent = allocations(m.get('annotations', {}).get('volcano.sh/vgpu-ids-new'))
    except ValueError as exc:
        return issue(str(exc), 'mismatch')
    if not permanent:
        return issue('Missing permanent UUID annotation')
    # These experiments explicitly request all three logical resource dimensions.
    # API DecimalSI quantities such as 1k mean 1000 resource units, not bytes.
    def quantity(value):
        match = re.fullmatch(r'([0-9]+)([kMGTPE]?)', str(value))
        if not match:
            raise ValueError('Unsupported integer resource quantity: '+str(value))
        return int(match[1])*10**{'':0, 'k':3, 'M':6, 'G':9, 'T':12, 'P':15, 'E':18}[match[2]]
    for ci, container in enumerate(sp.get('containers', [])):
        resources = container.get('resources', {})
        limits = resources.get('limits', {})
        entries = [a for a in permanent if a['container_index'] == ci]
        if not entries and 'volcano.sh/vgpu-number' not in limits:
            continue
        try:
            requested = {k: quantity(limits['volcano.sh/vgpu-'+k]) for k in ('number', 'memory', 'cores')}
            for key, value in resources.get('requests', {}).items():
                if key in ('volcano.sh/vgpu-number', 'volcano.sh/vgpu-memory', 'volcano.sh/vgpu-cores') and quantity(value) != requested[key.rsplit('-', 1)[-1]]:
                    return issue('Pod GPU requests/limits disagree', 'mismatch')
        except (KeyError, ValueError) as exc:
            return issue('Cannot verify explicit Pod quota: '+str(exc))
        if len(entries) != requested['number'] or any(a['memory_mib'] != requested['memory'] or a['compute_quota'] != requested['cores'] for a in entries):
            return issue('Permanent assignment does not match Pod requested instance/memory/cores quota', 'mismatch')
    result['native_permanent_uuids'] = [a['uuid'] for a in permanent]
    cp = checkpoint_json if isinstance(checkpoint_json, dict) and 'entries' in checkpoint_json else checkpoint(checkpoint_json)
    consumed, container_proofs = [], []
    for ci in sorted({a['container_index'] for a in permanent}):
        if ci >= len(sp.get('containers', [])):
            return issue('Allocation refers to nonexistent container', 'mismatch')
        container = sp['containers'][ci]['name']
        rows = [e for e in cp.get('entries', []) if e.get('PodUID') == uid and e.get('ContainerName') == container
                and e.get('ResourceName') == 'volcano.sh/vgpu-number']
        if len(rows) != 1:
            return issue('Missing or ambiguous Device Plugin checkpoint entry for '+container)
        visible = (rows[0].get('allocate_envs') or {}).get('NVIDIA_VISIBLE_DEVICES')
        if not visible:
            return issue('No decoded AllocateResponse NVIDIA_VISIBLE_DEVICES; DeviceIDs are not physical UUID proof')
        actual = [v.strip() for v in visible.split(',')]
        expected = [a['uuid'] for a in permanent if a['container_index'] == ci]
        if actual != expected:
            return issue('Device Plugin/native UUID mismatch for '+container, 'mismatch')
        consumed.extend(actual)
        container_proofs.append(dict(container=container, consumed_uuids=actual,
                                     source='matching UID/container/vgpu-number AllocateResponse.NVIDIA_VISIBLE_DEVICES'))
    result.update(device_plugin_consumed_uuids=consumed, container_proofs=container_proofs)
    native = []
    snapshots = parse_snapshots(scheduler_log)
    contexts = _session_context(scheduler_log)
    for i, line in enumerate(scheduler_log.splitlines()):
        if 'rf-job UUID allocation' not in line:
            continue
        try:
            receipt = native_commit_receipt(line)
        except ValueError:
            continue
        if receipt['podUID'] == uid and receipt['pod'] == name:
            native.append(dict(receipt, line_number=i, raw_line=line, observed_at=_line_time(line),
                               session=contexts[i]))
    if not native:
        return issue('No native allocation log for the bound Pod UID')
    releases = [r for r in parse_decisions(scheduler_log)['releases']
                if r['pod_uid'] == uid and r['line_number'] < native[-1]['line_number']]
    cut = releases[-1]['line_number'] if releases else -1
    segment = [r for r in native if r['line_number'] > cut]
    result.update(native_attempt_count=len(native), rollback_release_count_before_final_allocation=len(releases),
                  final_segment_receipts=segment)
    def units(rows):
        return [(a['container_index'], a['uuid'], a['memory_mib'], a['compute_quota']) for a in rows]
    for r in segment:
        if r['node'] != sp['nodeName'] or units(r['allocations']) != units(permanent):
            return issue('Final allocation segment conflicts with the bound Pod UUID/quota/node', 'mismatch')
        if r['fallback_used']:
            return issue('Native fallback used; RF recommendation chain is not established')
    advice = []
    for line in rf_log.splitlines():
        try:
            value = json.loads(line[line.index('{'):])
        except (ValueError, json.JSONDecodeError):
            continue
        if value.get('event') == 'rf_advice' and value.get('pod', {}).get('uid') == uid:
            advice.append(value)
    proofs = []
    for r in segment:
        # RF RequestID is a deterministic request/snapshot hash. The same exact
        # request may be audited again at a different UTC time after a retry.
        occurrences = [a for a in advice if a.get('request_id') == r['requestID']]
        matches = {json.dumps({k: v for k, v in a.items() if k != 'utc'}, sort_keys=True): a for a in occurrences}
        if len(matches) != 1:
            return issue('Missing or conflicting RF advice for native requestID '+r['requestID'])
        a = next(iter(matches.values()))
        if a.get('pod', {}).get('namespace') != m['namespace'] or a.get('pod', {}).get('name') != m['name']:
            return issue('RF advice Pod identity differs despite matching UID', 'mismatch')
        if a.get('model_version') != MODEL or not a.get('rf_prediction_enabled') or not a.get('model_ready') or a.get('status') != 'ranked':
            return issue('RF advice does not establish frozen active model use', 'mismatch')
        assignments = a.get('assignments', [])
        expected = [(sp['containers'][x['container_index']]['name'], x['uuid'], x['memory_mib'], x['compute_quota']) for x in permanent]
        actual = [(x.get('container'), x.get('gpu_uuid'), x.get('memory_mib'), x.get('compute_quota')) for x in assignments]
        if actual != expected or any(x.get('node') != sp['nodeName'] or x.get('instances') != 1 for x in assignments):
            return issue('RF/native UUID, quota, container or node mismatch', 'mismatch')
        proofs.append(dict(request_id=r['requestID'], model_version=a['model_version'],
                           recommended_uuids=[x['gpu_uuid'] for x in assignments], rf_advice=a,
                           equivalent_audit_occurrences=len(occurrences)))
    request_ids = sorted({r['requestID'] for r in segment})
    selection = None
    selected_snapshot = None
    # A single final event supplies a precise log/session join. With several
    # surviving events, UUID equality may still be proven, but do not choose an
    # arbitrary timing/score record for the actually consumed allocation.
    selected = segment[0] if len(segment) == 1 else None
    if selected:
        group = m.get('annotations', {}).get('scheduling.k8s.io/group-name')
        job = m['namespace']+'/'+group if group else None
        selection = next((x for x in reversed(parse_decisions(scheduler_log)['job_selections'])
                          if x['job'] == job and x['line_number'] <= selected['line_number']
                          and x['session'] == selected['session']), None)
        selected_snapshot = next((s for s in snapshots if s['job'] == job and s['session'] == selected['session']), None)
    result.update(uuid_consistent=True, rf_proofs=proofs, matching_request_ids=request_ids,
                  selected_request_id=request_ids[0] if len(request_ids) == 1 else None,
                  selected_native_allocation=selected, selected_job_selection=selection,
                  hrrn_snapshot_at_allocation=selected_snapshot,
                  status='confirmed' if len(request_ids) == 1 else 'uuid_consistent_request_ambiguous',
                  boundary='Bound API Pod plus matching plugin checkpoint establish UUID consumption; allocation logs alone do not establish binding or atomic multi-Pod commit')
    return result
