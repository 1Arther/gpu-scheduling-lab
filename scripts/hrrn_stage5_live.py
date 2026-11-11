"""Bounded Stage5 real-cluster runner. Fixed manifests, sleep only, UID cleanup.

Only the installed one-shot SSH gate may dispatch this package. The frozen
NodeLock/full-Job incompatibility keeps complete-C disabled. All evidence is
retained; this program never deletes another user's workload or an audit log.
"""
import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'helpers'))
from staging import inventory, allocations
from round7_collect import checkpoint
from round7_window_preflight import gpu_demand, blockers

K = ['sudo', '-n', 'kubectl', '--kubeconfig=/etc/kubernetes/admin.conf']
SYSTEM_NS, TEST_NS = 'gpu-rf-system', 'gpu-rf-tests'
DEPLOYMENT = 'volcano-hrrn-stage5'
ORIGINAL_SHA = '9f2e1ae3359ce234fd048bc48aab082add99a686631808908b5ae518bdebf4f4'
HRRN_SHA = 'dbedbd61bb774a12c276f520d6fcdf990d262c8fb46cec57f59e098bede695fd'
MODEL_SHA = 'ee35cfabe30fffc28ff0be09d3738587d4f9bffbe26386e2a9fae5c5b9e80748'
WINDOW_SECONDS = 1200

# Exact saved parsed-record fingerprints independently matched to round8's
# final checkpoint summaries and release logs. These do NOT prove equality of
# the original, truncated DeviceID tails, or live kubelet device-manager memory.
ROUND8_CHECKPOINT_HISTORY = {
    '161c2cb4-796b-4427-a3d8-13e410029061': {
        '6a0123ba70114673c4746f05334087c18175757c646adc975ceb5dbf13a42146',
        'e1cc6b8dd27263753dd1b6997b5c2a526acf95d86faabea554335ff88f0c1163',
        '837585444e96733982d3fcede71a3ac4c09fbb53b1817631dbbefc0840eb1030'},
    '4035b39a-16c4-4bbc-865d-cf08c7cfb288': {
        '3e5ed38a780ae39ead5e0a78b16433b2ba2ce5c09f64d6875df90ade8284a322',
        '56ba98ef8f6a0355dc790ffb69cacbc24786083316db6f5aa526f560c094436d',
        'a7c6562367fb13e711c67072179ebc664bcf8a2ce088caca9322f594053a1473'},
    '2f80a4b1-b58a-489d-860f-a4bd6aee87ac': {
        'e0bc14f8d274b7534b4b24856b13adf12ee5c9865cf569ba3646b10092d4bf99',
        'f1bb586f7778fd30234269666e629548107284e6f1034dd529ab5a1d3178ccf9',
        '4f7fbff5ce4f1ba2aaa07cea9eb596bc39fdb80183897d716f789216378e2228'},
    'b44dd9b5-cac7-4037-b77e-0a665537a009': {
        'c7f096ed473cff0ae42f16d506378288b651ba1cabd75813d80c968d4243c7b6',
        'ebe381ce5434095c527fea631cbdc0f6dcef476bbb89a3f75b6f3370f8dc0fbc',
        '6c571d94091b23b1bc8ea8ac9e5fc72ac087a9ab32dfc8b949f87ca616ba4548'},
}

# Independently reviewed r3 A receipts, after native release and UID deletion.
# Unlike round8, full raw records were retained and must ALSO match at runtime.
# Provenance: execution-live01-r3/r3-released-a-checkpoint-history.json;
# exported evidence SHA256 05e5f534ffbd9e8b658b05ef3a598082adcf884ce6ebf89112023c3c9ced36fc.
R3_CHECKPOINT_HISTORY = {
    'f3c9ba36-ec0e-498b-a3f0-aefea2150e0f': {
        '7dc501a28801da310331b7ae60f5e6b63a8c7042201d3235166a80914fdc8e09',
        '6c58e2fc1e93511a82738d9bb7a0ad0a7aa583ea829927fb812b26036716435b',
        'ea0fa082d15b90b16a0b9f30231aebfe6bf36da0652b77c159a32b0af1284e7d'},
    '05eb85cc-0d89-4b64-8780-339bac389324': {
        'caa06d708d43f830f1a7fde0320f67866f3e6c3579d4e7fa73b6a1d79c9a19ee',
        '1c2ea5a5824c8a533f119e5e7556fac248e9d45d3a15f004edf31266a24bf0fe',
        '01728820a747e8de4a7e468bbc394a0b057a384785395ead6fbbc302dc0e5f34'},
    '695e302d-cf09-4536-854b-bfa7fe0f9750': {
        'e9f6f0ffe72fe6f979171c5813e4df069280e108a563238635c01377ae61ac0d',
        'ad957fe5464af7fc6ff1b31485225de9ff9db0b8b06566bc07e9176edafb84ac',
        '1deec11253bb259e58d397ffaae52627213323345afe25f63a19b82078951219'},
    'b3bdf951-c672-4ba1-8fea-13c1666f348b': {
        'f6a234660b286e4da38591efffe83d708a95172f2fdda5b06434b64aea6dfdd9',
        '5327ce2ed720864eb7f039c0da3946da3f7180678aff319e0757667f3dcbfdae',
        '875e5444ff2d41382683677cec81000ff889cf88fb86c8ea5a5adf2d3813a0d3'},
}


R3_FULL_RAW_CHECKPOINT_HISTORY = {
    '05eb85cc-0d89-4b64-8780-339bac389324': {
        'volcano.sh/vgpu-memory': 'd2e05ace31addf52318ace7c1ee31288c5674cff3879b5b33c6ce8d2a86225cf',
        'volcano.sh/vgpu-number': '54efde0f9d672aaf163b06ac4a5592165417394c848c11889584a8e178dc2b18',
        'volcano.sh/vgpu-cores': 'ad9fce6ab55c0471a3304d51cd13008666a497ccf21754d29657ac4663077c2c',
    },
    '695e302d-cf09-4536-854b-bfa7fe0f9750': {
        'volcano.sh/vgpu-cores': 'e19a91625bdc4a569c048129c8b6cc3b5a1bad1466d9e73a5fe9c7af1317799e',
        'volcano.sh/vgpu-memory': '6938e1fd345f8c403efc93b0bb03e7e4a63c14c145072acd4aa5cd1085c9e637',
        'volcano.sh/vgpu-number': 'b56a36353eeb5c76f8c4ec508415c9c3fe4f78555bca7cc58067e50d30710cd6',
    },
    'b3bdf951-c672-4ba1-8fea-13c1666f348b': {
        'volcano.sh/vgpu-cores': '2344b191528f5f097514b61d66555d6c010af41a8700bd6683f0dd361e677bc8',
        'volcano.sh/vgpu-memory': '15f9edfcec3e7601e7f410ee0fd56a87750a8ecf39316ce96977d82a623ce2b7',
        'volcano.sh/vgpu-number': '67c0a3ff9b60074acde74699f54aaf6002fccb93b06eec4a4963eea6354ca23b',
    },
    'f3c9ba36-ec0e-498b-a3f0-aefea2150e0f': {
        'volcano.sh/vgpu-memory': 'a81ff6cb1b35db16d288052e919acd238dae5656453d58798c12bc2008a9e99b',
        'volcano.sh/vgpu-number': 'df56508c7b5af4bc4c110f1790f9bdbb7576bdfe540f5e5ea114f765bc4a7e46',
        'volcano.sh/vgpu-cores': '3cd03293771cff04ec4cbf93cf46559b7af1c286fdee9a08b35010ae00f0f45f',
    },
}

class OwnWorkloadTerminating(RuntimeError):
    """Only already-deleted experiment Pods remain in node/runtime views."""


def checkpoint_read_timeout(deadline):
    remaining = 35 if deadline is None else deadline - time.monotonic()
    if remaining <= 0:
        raise RuntimeError('Timed out waiting for experiment workload removal')
    return min(35, remaining)


def verify_r3_full_raw_receipts(entries):
    """No parsed/truncated summary can authorize an altered full r3 receipt."""
    seen, verified = set(), set()
    for entry in entries:
        uid = entry.get('PodUID')
        if uid not in R3_FULL_RAW_CHECKPOINT_HISTORY:
            continue
        identity = (uid, entry.get('ContainerName'), entry.get('ResourceName'))
        expected = R3_FULL_RAW_CHECKPOINT_HISTORY[uid].get(identity[2])
        actual = hashlib.sha256(json.dumps(entry, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        if identity in seen or identity[1] != 'sleep' or not expected or actual != expected:
            raise RuntimeError('Changed or duplicate full r3 historical checkpoint receipt')
        seen.add(identity)
        verified.add(uid)
    return sorted(verified)


def classify_checkpoint_receipts(entries, api_uids, kubelet_uids, active_cri_uids,
                                ready_sandbox_uids, own_uids=(), verified_r3_raw_uids=()):
    """Classify disk receipts using independently observed workload lifetimes."""
    own_uids = set(own_uids)
    observed_active = set(api_uids) | set(kubelet_uids) | set(active_cri_uids) | set(ready_sandbox_uids)
    relevant_uids = own_uids | set(ROUND8_CHECKPOINT_HISTORY) | set(R3_CHECKPOINT_HISTORY)
    relevant_active = observed_active & relevant_uids
    rows, seen, blocked = [], set(), bool(relevant_active)
    for entry in entries:
        uid = entry.get('PodUID')
        identity = (uid, entry.get('ContainerName'), entry.get('ResourceName'))
        fingerprint = hashlib.sha256(json.dumps(entry, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        if not all(identity) or identity in seen:
            classification = 'invalid_or_duplicate_receipt'
        elif uid in observed_active:
            classification = 'active_workload_receipt'
        elif uid in own_uids:
            classification = 'experiment_receipt_after_confirmed_workload_removal'
        elif fingerprint in ROUND8_CHECKPOINT_HISTORY.get(uid, set()):
            classification = 'verified_round8_saved_summary_history'
        elif uid in verified_r3_raw_uids and fingerprint in R3_CHECKPOINT_HISTORY.get(uid, set()):
            classification = 'verified_r3_saved_full_record_history'
        else:
            classification = 'unknown_or_changed_receipt'
        seen.add(identity)
        accepted = classification in ('experiment_receipt_after_confirmed_workload_removal',
                                      'verified_round8_saved_summary_history',
                                      'verified_r3_saved_full_record_history')
        blocked = blocked or not accepted
        rows.append(dict(pod_uid=uid, container=identity[1], resource=identity[2],
                         parsed_record_sha256=fingerprint, classification=classification, accepted=accepted))
    return dict(accepted=not blocked, entries=rows, active_relevant_uids=sorted(relevant_active),
                api_present_uids=sorted(set(api_uids) & relevant_uids),
                kubelet_present_uids=sorted(set(kubelet_uids) & relevant_uids),
                current_cri_uids=sorted(set(active_cri_uids) & relevant_uids),
                ready_sandbox_uids=sorted(set(ready_sandbox_uids) & relevant_uids),
                r3_full_raw_records_verified_uids=sorted(verified_r3_raw_uids),
                kubelet_device_manager_memory_directly_observed=False,
                full_historical_raw_device_id_sets_proven_equal=False,
                disk_checkpoint_is_live_allocation_table=False,
                boundary='Kubelet v1.35 UpdateAllocatedDevices removes inactive Pod allocations in memory without writing this checkpoint; disk receipts may persist. Workload removal is independently observed, not inferred from checkpoint disappearance.')


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def save(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + '\n')
    temp.replace(path)


def pod_ref(p):
    m = p['metadata']
    return dict(kind=p['kind'], name=m['name'], namespace=m.get('namespace'), uid=m['uid'])


def validate_execution_scope(cases):
    enabled = [c for c in cases if c.get('execution_enabled')]
    actual = [(c.get('id'), c.get('mode'), c.get('kind')) for c in enabled]
    expected = {('order-a', 'A', 'order'), ('order-b', 'B', 'order')}
    if len(actual) != 2 or set(actual) != expected:
        raise RuntimeError('Only one native A and one HRRN B ordering case may execute')
    return enabled


def validate_decisive_hrrn(comparison, proof, pod, long_job, short_job, minimum_wait_seconds=15):
    """Validate B using the consumed allocation's Session, never an earlier score.

    The frozen binary does not log p.now or HRRN comparator returns.  Reconstruct
    the shared clock from readyAt + waitingSeconds with a 1 microsecond tolerance
    for float serialization; label this inference separately from logged values.
    """
    import math
    from decimal import Decimal
    from hrrn_stage5_evidence import _nanoseconds

    def require(condition, reason):
        if not condition:
            raise RuntimeError('Decisive HRRN evidence invalid: ' + reason)

    def finite(value):
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)

    require(finite(minimum_wait_seconds) and minimum_wait_seconds >= 15, 'waiting threshold must be at least 15 seconds')
    allocation = proof.get('selected_native_allocation') or {}
    selection = proof.get('selected_job_selection') or {}
    session = allocation.get('session')
    metadata, spec = pod.get('metadata', {}), pod.get('spec', {})
    pod_name = metadata.get('namespace', '') + '/' + metadata.get('name', '')
    group = metadata.get('namespace', '') + '/' + metadata.get('annotations', {}).get('scheduling.k8s.io/group-name', '')
    require(proof.get('status') == 'confirmed' and proof.get('uuid_consistent') is True,
            'RF/native/Device Plugin consumption is not confirmed')
    require(bool(session) and comparison.get('session') == session, 'comparison does not use the consumed allocating Session')
    require(group == short_job and spec.get('nodeName') and pod.get('status', {}).get('phase') == 'Running',
            'actually running first Pod must belong to the short Job')
    require(bool(metadata.get('uid')) and proof.get('pod_uid') == metadata['uid'] == allocation.get('podUID')
            and proof.get('pod') == pod_name == allocation.get('pod') and allocation.get('node') == spec['nodeName'],
            'consumed allocation Pod UID/name/node differs from first running Pod')
    require(len(proof.get('final_segment_receipts', [])) == 1
            and proof['final_segment_receipts'][0] == allocation
            and proof.get('selected_request_id') == allocation.get('requestID'),
            'final consumed allocation request is missing or ambiguous')
    assigned = [a['uuid'] for a in allocations(metadata.get('annotations', {}).get('volcano.sh/vgpu-ids-new'))]
    require(bool(assigned) and assigned == proof.get('native_permanent_uuids') == proof.get('device_plugin_consumed_uuids')
            and assigned == [a['uuid'] for a in allocation.get('allocations', [])],
            'running Pod/permanent native/consumed Device Plugin UUIDs differ')
    rf = proof.get('rf_proofs', [])
    require(len(rf) == 1 and rf[0].get('request_id') == allocation.get('requestID')
            and rf[0].get('recommended_uuids') == assigned, 'RF recommendation does not match the consumed allocation')
    allocation_line, selection_line = allocation.get('line_number'), selection.get('line_number')
    require(isinstance(allocation_line, int) and isinstance(selection_line, int)
            and 0 <= selection_line <= allocation_line and selection.get('session') == session
            and selection.get('job') == short_job, 'short Job selection is not linked to the consumed allocation')
    require(comparison.get('all_three_preceding_plugins_have_tie_evidence') is True
            and comparison.get('any_preceding_non_tie') is False,
            'priority/gang/DRF do not establish ties in the decisive Session')
    snapshots = comparison.get('snapshots') or {}
    require(set(snapshots) == {long_job, short_job} and long_job != short_job,
            'both decisive Job snapshots are required')
    reconstructed = {}
    for job, expected in ((long_job, 100), (short_job, 10)):
        row = snapshots[job]
        require(row.get('session') == session and row.get('job') == job and row.get('ready') is True,
                'Job is not fully ready in the decisive Session: ' + job)
        line = row.get('line_number')
        require(isinstance(line, int) and 0 <= line <= selection_line,
                'snapshot is not before actual short Job selection: ' + job)
        wait, estimate, score = row.get('waiting_seconds'), row.get('estimated_seconds'), row.get('score')
        require(all(finite(x) for x in (wait, estimate, score)) and wait >= minimum_wait_seconds,
                'nonzero recorded wait is below the fixed threshold or invalid: ' + job)
        require(estimate == expected and row.get('reason') == 'complete_observed;podgroup_annotation',
                'expected service must come from the fixed 100/10 second PodGroup annotation: ' + job)
        require(math.isclose(score, 1 + wait / max(estimate, 1.0), rel_tol=1e-12, abs_tol=1e-12),
                'logged score does not satisfy frozen HRRN formula (tau=1): ' + job)
        ready_ns = _nanoseconds(row.get('ready_at'))
        require(ready_ns is not None, 'missing precise readyAt timestamp: ' + job)
        reconstructed[job] = Decimal(ready_ns) + Decimal(str(wait)) * Decimal(10**9)
    skew = abs(reconstructed[long_job] - reconstructed[short_job])
    require(skew <= 1000, 'readyAt plus waitingSeconds do not reconstruct one Session clock')
    require(proof.get('hrrn_snapshot_at_allocation') == snapshots[short_job],
            'allocation snapshot is not the short Job snapshot from the decisive Session')
    require(snapshots[short_job]['score'] > snapshots[long_job]['score'] and comparison.get('hrrn_result') == 1,
            'short Job does not have the higher decisive HRRN score')
    return dict(validated=True, session=session, minimum_wait_seconds=minimum_wait_seconds,
                estimated_seconds={long_job: 100, short_job: 10}, tau_seconds=1.0,
                snapshots=snapshots, consumed_pod_uid=metadata['uid'], consumed_uuids=assigned,
                native_request_id=allocation['requestID'], reconstructed_session_ns={k: str(v) for k, v in reconstructed.items()},
                reconstructed_session_skew_ns=str(skew), reconstructed_session_tolerance_ns=1000,
                session_time_source='derived_ready_at_plus_waiting_seconds_not_direct_p_now_log',
                hrrn_comparator_source='derived_from_snapshots_and_frozen_source_no_direct_HRRN_compare_log')


def process_identity(pid):
    path = Path(f'/proc/{int(pid)}')
    try:
        fields = (path / 'stat').read_text().rsplit(')', 1)[1].split()
        if fields[0] == 'Z':
            return None
        return dict(pid=int(pid), start_ticks=int(fields[19]), uid=path.stat().st_uid,
                    argv=[part.decode() for part in (path / 'cmdline').read_bytes().split(b'\0') if part])
    except FileNotFoundError:
        return None


def signal_owned_worker(expected, sig, runner_path):
    actual = process_identity(expected['pid'])
    if actual is None:
        return False
    if actual != expected or actual['uid'] != os.getuid() or str(runner_path) not in actual['argv'] or '--execute' not in actual['argv']:
        raise RuntimeError('Refuse signal: runner identity changed or is unrelated')
    os.kill(actual['pid'], sig)
    return True


def acquire_recovery_lock(lock, timeout_seconds=150, clock=time.monotonic, sleeper=time.sleep):
    deadline = clock() + timeout_seconds
    while True:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if clock() >= deadline:
                raise TimeoutError('Bounded recovery lock acquisition timed out')
            sleeper(1)


def object_path(row):
    plural = {'Pod': ('/api/v1', 'pods'), 'ConfigMap': ('/api/v1', 'configmaps'),
              'Deployment': ('/apis/apps/v1', 'deployments'),
              'PodGroup': ('/apis/scheduling.volcano.sh/v1beta1', 'podgroups'),
              'Job': ('/apis/batch.volcano.sh/v1alpha1', 'jobs'),
              'Lease': ('/apis/coordination.k8s.io/v1', 'leases')}
    group, resource = plural[row['kind']]
    return group + '/namespaces/' + row['namespace'] + '/' + resource + '/' + row['name']


class Run:
    def __init__(self, root=ROOT):
        self.root = root
        self.protocol = json.loads((root / 'protocol.json').read_text())
        self.run_id = self.protocol['run_id']
        self.out = root / 'evidence'
        self.out.mkdir(exist_ok=True)
        self.state_path = self.out / 'state.json'
        self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else dict(
            run_id=self.run_id, phase='new', created=[], planned=[], cases=[], old_scale_intent=False,
            experiment_started=False, restored=False, observations={})
        self.counter = len(list((self.out / 'commands').glob('*.json')))
        self.recovering = False

    def save(self):
        save(self.state_path, self.state)

    def event(self, event, **data):
        with (self.out / 'events.jsonl').open('a') as stream:
            stream.write(json.dumps(dict(utc=now(), event=event, **data)) + '\n')
        (self.out / 'heartbeat').touch()

    def cmd(self, args, body=None, check=True, timeout=35, capture_full=False):
        self.event('command', argv=args)
        start = now()
        try:
            p = subprocess.run(args, input=None if body is None else json.dumps(body),
                               capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            self.event('command_timeout', argv=args, timeout=timeout)
            raise RuntimeError('Bounded command timeout: ' + ' '.join(args)) from exc
        self.counter += 1
        row = dict(start=start, end=now(), argv=args, stdin=body, exit_code=p.returncode,
                   stdout_sha256=hashlib.sha256(p.stdout.encode()).hexdigest(), stderr=p.stderr[-20000:])
        if capture_full:
            row['stdout'] = p.stdout
        save(self.out / 'commands' / f'{self.counter:06}.json', row)
        if check and p.returncode:
            raise RuntimeError('Command failed: ' + ' '.join(args) + ' ' + p.stderr[-1000:])
        return p

    def get(self, kind, name=None, namespace=None, optional=False):
        # Job always denotes Volcano Job here; never an ambiguous batch Job.
        kind = {'Job': 'jobs.batch.volcano.sh', 'Pod': 'pods', 'PodGroup': 'podgroups.scheduling.volcano.sh',
                'Deployment': 'deployments', 'ConfigMap': 'configmaps', 'Lease': 'leases.coordination.k8s.io'}.get(kind, kind)
        args = K + ['get', kind] + ([name] if name else [])
        args += ['-n', namespace] if namespace else (['-A'] if kind in ('pods', 'podgroups', 'deployments,statefulsets,daemonsets,replicasets,jobs.batch,cronjobs.batch', 'resourceclaims.resource.k8s.io') else [])
        if optional:
            args += ['--ignore-not-found']
        p = self.cmd(args + ['-o', 'json', '--request-timeout=15s'])
        return json.loads(p.stdout) if p.stdout.strip() else None

    def backup(self, name, obj):
        path = self.out / 'backups' / (name + '.json')
        if path.exists():
            raise RuntimeError('Refuse backup overwrite: ' + name)
        save(path, obj)

    def prior(self, name):
        return json.loads((self.out / 'backups' / (name + '.json')).read_text())

    def is_ours(self, obj):
        return obj['metadata'].get('labels', {}).get('gpu-lab/run-id') == self.run_id

    def adopt_pending_creates(self):
        known = {o['uid'] for o in self.state['created']}
        for plan in self.state['planned']:
            obj = self.get(plan['kind'], plan['name'], plan['namespace'], optional=True)
            if not obj or obj['metadata']['uid'] in known:
                continue
            token = obj['metadata'].get('annotations', {}).get('gpu-lab/create-token')
            if not self.is_ours(obj) or token != plan['create_token']:
                raise RuntimeError('Uncertain create has a different identity: ' + plan['name'])
            row = pod_ref(obj)
            self.state['created'].append(row)
            known.add(row['uid'])
            self.save()
            self.event('uncertain_create_adopted', **row)

    def create(self, original):
        obj = json.loads(json.dumps(original))
        m = obj['metadata']
        if not self.is_ours(obj) or m.get('namespace') not in (SYSTEM_NS, TEST_NS):
            raise RuntimeError('Manifest outside this experiment')
        allowed = {(o['kind'], o['metadata']['namespace'], o['metadata']['name']) for o in self.protocol['setup_objects']}
        allowed |= {(o['kind'], o['metadata']['namespace'], o['metadata']['name'])
                    for case in validate_execution_scope(self.protocol['cases']) for docs in case['jobs'].values() for o in docs}
        allowed.add(('Lease', SYSTEM_NS, DEPLOYMENT))
        if (obj['kind'], m['namespace'], m['name']) not in allowed:
            raise RuntimeError('Object is outside the frozen first-tranche manifest identities')
        if self.get(obj['kind'], m['name'], m['namespace'], optional=True):
            raise RuntimeError('Refuse to replace an existing object: ' + m['name'])
        token = str(uuid.uuid4())
        m.setdefault('annotations', {})['gpu-lab/create-token'] = token
        self.state['planned'].append(dict(kind=obj['kind'], name=m['name'], namespace=m['namespace'], create_token=token))
        self.save()  # durable intent before a possibly ambiguous API response
        result = json.loads(self.cmd(K + ['create', '-f', '-', '-o', 'json', '--request-timeout=20s'], obj).stdout)
        row = pod_ref(result)
        self.state['created'].append(row)
        self.save()
        save(self.out / 'created' / (row['uid'] + '.json'), result)
        self.event('created', **row)
        return result

    def delete(self, row):
        if row not in self.state['created']:
            raise RuntimeError('Refuse deletion of an object absent from the durable created ledger')
        obj = self.get(row['kind'], row['name'], row['namespace'], optional=True)
        if not obj:
            return
        lease_owned = row['kind'] == 'Lease' and row['uid'] == self.state.get('lease_uid')
        if lease_owned:
            holder = obj.get('spec', {}).get('holderIdentity', '').split('_', 1)[0]
            lease_owned = not holder or holder in self.state.get('scheduler_pod_names', [])
        if obj['metadata']['uid'] != row['uid'] or not (self.is_ours(obj) or lease_owned):
            raise RuntimeError('Refuse replaced/foreign object deletion: ' + row['name'])
        body = dict(apiVersion='v1', kind='DeleteOptions', preconditions=dict(uid=row['uid']),
                    propagationPolicy='Foreground', gracePeriodSeconds=5)
        self.cmd(K + ['delete', '--raw=' + object_path(row), '-f', '-'], body)
        self.wait(lambda: self.get(row['kind'], row['name'], row['namespace'], optional=True) is None,
                  65, check_guard=False)
        self.event('deleted', **row)

    def observe(self, pods):
        for p in pods:
            if not self.is_ours(p):
                continue
            m, s = p['metadata'], p.get('status', {})
            row = dict(utc=now(), uid=m['uid'], name=m['name'], namespace=m['namespace'],
                       node=p['spec'].get('nodeName'), phase=s.get('phase'),
                       annotations=m.get('annotations', {}), status=s)
            key = m['uid']
            stable = json.dumps({k: v for k, v in row.items() if k != 'utc'}, sort_keys=True)
            if self.state['observations'].get(key) != stable:
                self.state['observations'][key] = stable
                with (self.out / 'pod-observations.jsonl').open('a') as stream:
                    stream.write(json.dumps(row) + '\n')
                save(self.out / 'last-pods' / (key + '.json'), p)
        self.save()

    def guard(self):
        if (self.root / 'STOP').exists() and not self.recovering:
            raise RuntimeError('Operator stop requested')
        if self.state.get('started_epoch') and not self.recovering and time.time() - self.state['started_epoch'] > WINDOW_SECONDS:
            raise RuntimeError('Bounded maintenance window exceeded')
        pods = self.get('pods')['items']
        self.observe(pods)
        own = {o['uid'] for o in self.state['created'] if o['kind'] == 'Pod'}
        foreign = [p['metadata']['namespace'] + '/' + p['metadata']['name'] for p in pods
                   if p.get('status', {}).get('phase') not in ('Succeeded', 'Failed')
                   and gpu_demand(p) and p['metadata']['uid'] not in own]
        finite_foreign = [p for p in blockers(pods, 't3dgq')['finite_plugin_candidates'] if p['uid'] not in own]
        if (foreign or finite_foreign) and not self.recovering:
            self.state['foreign_conflict'] = dict(active=foreign, pending_allocations=finite_foreign, utc=now())
            self.save()
            raise RuntimeError('Competing GPU/DRA request; stop new tests: ' + str(foreign))
        if not self.recovering:
            if self.state.get('case_deadline_epoch') and time.time() > self.state['case_deadline_epoch']:
                raise RuntimeError('Case exceeded its declared deadline')
            byuid = {p['metadata']['uid']: p for p in pods}
            for uid in self.state.get('active_holder_uids', []):
                holder = byuid.get(uid)
                if not holder or holder.get('status', {}).get('phase') in ('Succeeded', 'Failed') or holder['metadata'].get('deletionTimestamp'):
                    raise RuntimeError('A controlled holder ended unexpectedly; ordering conditions changed')
            scheduler_uid = self.state.get('scheduler_pod_uid')
            if scheduler_uid:
                scheduler = byuid.get(scheduler_uid)
                if not scheduler or any(c.get('restartCount', 0) for c in scheduler.get('status', {}).get('containerStatuses', [])):
                    raise RuntimeError('Experiment scheduler restarted; real waiting history invalidated')
        if self.state.get('experiment_started') and not self.recovering:
            original = self.get('deployment', 'volcano-scheduler', 'volcano-system')
            if original['spec']['replicas'] != 0 or any(p['metadata']['namespace'] == 'volcano-system'
                    and p['metadata']['name'].startswith('volcano-scheduler-') for p in pods):
                raise RuntimeError('Original scheduler active during experiment')
        return pods

    def wait(self, condition, seconds=90, check_guard=True):
        end = time.monotonic() + seconds
        while True:
            if check_guard:
                self.guard()
            result = condition()
            if result:
                return result
            if time.monotonic() > end:
                raise RuntimeError('Timed out waiting: ' + getattr(condition, '__name__', 'condition'))
            time.sleep(1)

    def snapshot(self, name):
        pods = self.guard()
        node = self.get('node', 't3dgq')
        devices, warnings = inventory([node], pods)
        if warnings or len(devices) != 4:
            raise RuntimeError('Unreconciled device state: ' + str(warnings))
        if any(min(d['free_memory_mib'], d['free_compute_quota'], d['free_instances']) < 0 for d in devices):
            raise RuntimeError('Logical over-allocation')
        result = dict(utc=now(), devices=devices, node_lock=node['metadata'].get('annotations', {}).get('hamivgpu'))
        save(self.out / 'snapshots' / (name + '.json'), dict(**result, pods=pods))
        return result

    def process_sha(self, container_name, namespace, expected):
        data = json.loads(self.cmd(['sudo', '-n', 'crictl', 'ps', '-o', 'json']).stdout)
        matches = [c for c in data.get('containers', []) if c.get('metadata', {}).get('name') == container_name
                   and c.get('labels', {}).get('io.kubernetes.pod.namespace') == namespace]
        if len(matches) != 1:
            raise RuntimeError('Expected one scheduler process: ' + str(matches))
        info = json.loads(self.cmd(['sudo', '-n', 'crictl', 'inspect', '-o', 'json', matches[0]['id']]).stdout)
        pid = int(info['info']['pid'])
        value = self.cmd(['sudo', '-n', 'sha256sum', f'/proc/{pid}/exe']).stdout.split()[0]
        if value != expected:
            raise RuntimeError('Running scheduler ELF SHA mismatch')
        self.event('running_binary_verified', container=container_name, container_id=matches[0]['id'], pid=pid, sha256=value)
        return value

    def prepare(self):
        if self.state['phase'] != 'new':
            raise RuntimeError('This attempt was already prepared; preserve its evidence')
        self.state['preflight_attempts'] = self.state.get('preflight_attempts', 0) + 1
        self.save()
        attempt = str(self.state['preflight_attempts'])
        # Fresh preflight, not the old desktop observation.
        snapshot = self.snapshot('initial-' + attempt)
        if snapshot['node_lock'] or any(d['allocated_vgpu_count'] for d in snapshot['devices']):
            raise RuntimeError('Node has logical reservations or lock')
        self.reconcile_checkpoint('preflight-' + attempt)
        gpu = self.cmd(['nvidia-smi'], capture_full=True).stdout
        (self.out / ('nvidia-before-' + attempt + '.txt')).write_text(gpu)
        if 'No running processes found' not in gpu:
            raise RuntimeError('GPU processes present; do not stop them')
        node = self.get('node', 't3dgq')
        if not any(c['type'] == 'Ready' and c['status'] == 'True' for c in node['status']['conditions']):
            raise RuntimeError('Target node not Ready')
        if node['spec'].get('unschedulable'):
            raise RuntimeError('Target node cordoned')
        claims = self.get('resourceclaims.resource.k8s.io')['items']
        if claims:
            raise RuntimeError('DRA claims require separate reconciliation')
        controllers = self.get('deployments,statefulsets,daemonsets,replicasets,jobs.batch,cronjobs.batch')['items']
        for c in controllers:
            spec = c.get('spec', {})
            template = spec.get('jobTemplate', {}).get('spec', spec).get('template', {}).get('spec', {})
            if gpu_demand({'spec': template}):
                raise RuntimeError('GPU controller template exists: ' + c['metadata']['name'])
        backups = {}
        for key, kind, name, ns in [
            ('original', 'deployment', 'volcano-scheduler', 'volcano-system'),
            ('original-config', 'configmap', 'volcano-scheduler-configmap', 'volcano-system'),
            ('rf', 'deployment', 'volcano-rf-test', SYSTEM_NS),
            ('dqn', 'deployment', 'dqn-scheduler-grpc', 'volcano-system'),
            ('quota', 'resourcequota', 'rf-v7-test-budget', TEST_NS),
            ('rf-config', 'configmap', 'rf-v7-config-on', SYSTEM_NS),
            ('geometry', 'configmap', 'rf-v7-device-config', SYSTEM_NS)]:
            backups[key] = self.get(kind, name, ns)
        save(self.out / 'preflight' / (attempt + '-observed-originals.json'), backups)
        if backups['original']['spec']['replicas'] != 1 or backups['original']['status'].get('availableReplicas') != 1:
            raise RuntimeError('Original scheduler not healthy 1/1')
        if backups['rf']['spec']['replicas'] != 0:
            raise RuntimeError('Independent RF scheduler is active')
        self.process_sha('volcano-scheduler', 'volcano-system', ORIGINAL_SHA)
        model = '/home/zbs/rf-round7-live-window-20261019/lab/models/pod-gpu-rf-v1/random_forest.joblib'
        if self.cmd(['sha256sum', model]).stdout.split()[0] != MODEL_SHA:
            raise RuntimeError('Frozen RF model changed')
        for verb, resource, ns in [('patch', 'deployments', 'volcano-system'),
                                   ('patch', 'resourcequotas', TEST_NS), ('create', 'deployments', SYSTEM_NS),
                                   ('create', 'configmaps', SYSTEM_NS), ('create', 'pods', TEST_NS),
                                   ('delete', 'pods', TEST_NS), ('create', 'podgroups.scheduling.volcano.sh', TEST_NS)]:
            answer = self.cmd(K + ['auth', 'can-i', verb, resource, '-n', ns, '--request-timeout=15s']).stdout.strip()
            if answer != 'yes':
                raise RuntimeError('Missing execution permission: ' + resource)
        for verb, resource, ns, subresource in [('create', 'leases.coordination.k8s.io', SYSTEM_NS, None),
                                   ('get', 'leases.coordination.k8s.io', SYSTEM_NS, None),
                                   ('update', 'leases.coordination.k8s.io', SYSTEM_NS, None),
                                   ('get', 'nodes/t3dgq', SYSTEM_NS, None), ('patch', 'nodes/t3dgq', SYSTEM_NS, None),
                                   ('patch', 'podgroups.scheduling.volcano.sh', TEST_NS, None),
                                   ('update', 'podgroups.scheduling.volcano.sh', TEST_NS, None),
                                   ('create', 'pods', TEST_NS, 'binding'), ('patch', 'pods', TEST_NS, None)]:
            # TYPE/NAME means a resource name to kubectl auth can-i, not a
            # subresource. The scheduler may bind Pods without creating Pods.
            args = K + ['auth', 'can-i', verb, resource, '-n', ns,
                        '--as=system:serviceaccount:gpu-rf-system:volcano-rf-test', '--request-timeout=15s']
            if subresource:
                args.append('--subresource=' + subresource)
            answer = self.cmd(args).stdout.strip()
            if answer != 'yes':
                raise RuntimeError('Existing experiment SA lacks required permission: ' + resource)
        if self.get('deployment', DEPLOYMENT, SYSTEM_NS, optional=True):
            raise RuntimeError('Experiment Deployment already exists')
        if self.get('lease', DEPLOYMENT, SYSTEM_NS, optional=True):
            raise RuntimeError('Experiment Lease already exists; do not adopt another run')
        images = {c['image'] for o in self.protocol['setup_objects'] if o['kind'] == 'Deployment'
                  for c in o['spec']['template']['spec']['containers'] if c['name'] != 'scheduler'}
        images |= {c['image'] for case in self.protocol['cases'] for docs in case['jobs'].values()
                   for o in docs if o['kind'] == 'Pod' for c in o['spec']['containers']}
        for image in sorted(images):
            self.cmd(['sudo', '-n', 'crictl', 'inspecti', image], capture_full=True)
        # Read-only attempts may fail and retry. Commit recovery originals only
        # after every preflight check succeeds; retain earlier observations.
        for key, obj in backups.items():
            self.backup(key, obj)
        self.state['phase'] = 'prepared'
        self.state['prepared_utc'] = now()
        self.save()
        self.event('prepared')

    def patch_replicas(self, name, ns, replicas, expected_uid=None):
        obj = self.get('deployment', name, ns)
        if expected_uid and obj['metadata']['uid'] != expected_uid:
            raise RuntimeError('Deployment identity changed')
        patch = [dict(op='test', path='/metadata/resourceVersion', value=obj['metadata']['resourceVersion']),
                 dict(op='test', path='/metadata/uid', value=obj['metadata']['uid']),
                 dict(op='replace', path='/spec/replicas', value=replicas)]
        self.cmd(K + ['patch', 'deployment', name, '-n', ns, '--type=json', '-p', json.dumps(patch), '--request-timeout=20s'])

    def scheduler_pods(self):
        return [p for p in self.get('pods')['items'] if p['metadata']['namespace'] == SYSTEM_NS
                and p['metadata'].get('labels', {}).get('app') == DEPLOYMENT]

    def scale_experiment(self, replicas):
        stopping = []
        if not replicas:
            stopping = self.scheduler_pods()
            if any(not self.is_ours(p) for p in stopping):
                raise RuntimeError('Unexpected experiment scheduler identity before scale-down')
            self.state.pop('scheduler_pod_uid', None)
            self.save()
        elif not self.state.get('lease_uid'):
            raise RuntimeError('Experiment Lease must be pre-created and its UID recorded')
        own = next(o for o in self.state['created'] if o['kind'] == 'Deployment')
        self.patch_replicas(DEPLOYMENT, SYSTEM_NS, replicas, own['uid'])
        if replicas:
            self.cmd(K + ['rollout', 'status', 'deployment/' + DEPLOYMENT, '-n', SYSTEM_NS, '--timeout=90s'], timeout=100)
            self.process_sha('scheduler', SYSTEM_NS, HRRN_SHA)
            pods = self.scheduler_pods()
            if len(pods) != 1 or not self.is_ours(pods[0]):
                raise RuntimeError('Unexpected experiment scheduler identity')
            pod_name, pod_uid = pods[0]['metadata']['name'], pods[0]['metadata']['uid']
            self.state['scheduler_pod_uid'] = pod_uid
            self.state.setdefault('scheduler_pod_names', []).append(pod_name)
            self.save()
            # /healthz becomes ready before RunOrDie obtains leadership. A stopped
            # leader leaves its Lease until natural expiry (ReleaseOnCancel=false).
            started, budget, attempt = time.monotonic(), 120.0, 0
            evidence = self.out / 'leader-acquisition' / (pod_uid + '-' + uuid.uuid4().hex)
            summary = dict(started_utc=now(), pod_name=pod_name, pod_uid=pod_uid,
                           expected_lease_uid=self.state['lease_uid'], status='waiting',
                           readiness_is_leadership=False, lease_or_clock_modified=False)
            try:
                while True:
                    self.guard()  # Includes the global maintenance deadline and Pod restart checks.
                    remaining = started + budget - time.monotonic()
                    if remaining <= 0:
                        raise RuntimeError('Timed out waiting for experiment Lease leadership')
                    lease = json.loads(self.cmd(K + ['get', 'lease', DEPLOYMENT, '-n', SYSTEM_NS,
                                                   '-o', 'json', '--request-timeout=15s'],
                                                capture_full=True, timeout=min(20, remaining)).stdout)
                    attempt += 1
                    elapsed = time.monotonic() - started
                    save(evidence / f'{attempt:04}.json', dict(observed_utc=now(), elapsed_seconds=elapsed,
                                                             lease=lease, wait_budget_seconds=budget))
                    if lease['metadata']['uid'] != self.state['lease_uid']:
                        raise RuntimeError('Experiment Lease UID changed unexpectedly')
                    holder = lease.get('spec', {}).get('holderIdentity') or ''
                    holder_name = holder.split('_', 1)[0]
                    duration = lease.get('spec', {}).get('leaseDurationSeconds')
                    if holder:
                        if not isinstance(duration, int) or isinstance(duration, bool) or duration <= 0:
                            raise RuntimeError('Held experiment Lease lacks a valid duration')
                        # Natural expiry plus a bounded client-go retry margin. Do
                        # not infer expiry from remote wall clocks or edit the Lease.
                        budget = min(budget, float(duration + max(10, min(30, duration / 4))))
                    if holder_name == pod_name:
                        if elapsed > budget:
                            raise RuntimeError('Experiment Lease acquired after bounded deadline')
                        self.state.setdefault('leader_holder_identities', {})[pod_name] = holder
                        self.state.setdefault('leader_pod_uids', {})[pod_name] = pod_uid
                        summary.update(status='acquired', acquired_utc=now(), holder_identity=holder,
                                       lease_duration_seconds=duration)
                        self.save()
                        return
                    if holder:
                        stopped = self.state.get('stopped_scheduler_pods', {}).get(holder_name, {})
                        known_identity = self.state.get('leader_holder_identities', {}).get(holder_name)
                        known_uid = self.state.get('leader_pod_uids', {}).get(holder_name)
                        if (holder != known_identity or not known_uid or stopped.get('uid') != known_uid
                                or not stopped.get('api_absent') or not stopped.get('current_cri_absent')):
                            raise RuntimeError('Experiment Lease has an unknown or unverified active holder')
                    remaining = started + budget - time.monotonic()
                    if remaining <= 0:
                        raise RuntimeError('Timed out waiting for experiment Lease leadership')
                    time.sleep(min(1, remaining))
            except BaseException as exc:
                summary.update(status='failed', error=repr(exc))
                raise
            finally:
                summary.update(elapsed_seconds=time.monotonic() - started, observation_count=attempt,
                               wait_budget_seconds=budget, finished_utc=now())
                save(evidence / 'summary.json', summary)
                self.event('leader_acquisition', **summary)
        else:
            self.wait(lambda: not self.scheduler_pods(), 65, check_guard=False)
            containers = json.loads(self.cmd(['sudo', '-n', 'crictl', 'ps', '-o', 'json']).stdout).get('containers', [])
            if any(c.get('labels', {}).get('io.kubernetes.pod.namespace') == SYSTEM_NS
                   and c.get('labels', {}).get('io.kubernetes.pod.name', '').startswith(DEPLOYMENT + '-') for c in containers):
                raise RuntimeError('Experiment process still running after scale-down')
            for pod in stopping:
                self.state.setdefault('stopped_scheduler_pods', {})[pod['metadata']['name']] = dict(
                    uid=pod['metadata']['uid'], verified_utc=now(), api_absent=True, current_cri_absent=True)
            self.save()

    def logs(self, name):
        pods = self.scheduler_pods()
        if len(pods) != 1:
            raise RuntimeError('Expected one experiment scheduler Pod for logs')
        result = {}
        for container in ('scheduler', 'rf-model'):
            p = self.cmd(K + ['logs', '-n', SYSTEM_NS, pods[0]['metadata']['name'], '-c', container,
                             '--timestamps', '--limit-bytes=16000000', '--request-timeout=20s'], timeout=35)
            path = self.out / 'logs' / (name + '-' + container + '.log')
            path.parent.mkdir(exist_ok=True)
            path.write_text(p.stdout)
            result[container] = p.stdout
        return result

    def checkpoint(self, name, deadline=None):
        path = '/var/lib/kubelet/device-plugins/kubelet_internal_checkpoint'
        def read(args):
            return self.cmd(args, capture_full=True, timeout=checkpoint_read_timeout(deadline)).stdout
        before = read(['sudo', '-n', 'stat', '-c', '%s %Y %y %i', path]).strip()
        text = read(['sudo', '-n', 'cat', path])
        after = read(['sudo', '-n', 'stat', '-c', '%s %Y %y %i', path]).strip()
        raw_file = self.out / 'checkpoints' / (name + '-raw-' + uuid.uuid4().hex + '.json')
        raw_file.parent.mkdir(parents=True, exist_ok=True)
        raw_file.write_bytes(text.encode())
        raw = json.loads(text)
        inner = raw.get('Data', raw)
        if isinstance(inner, str):
            inner = json.loads(inner)
        if (not isinstance(inner, dict) or 'PodDeviceEntries' not in inner or 'RegisteredDevices' not in inner
                or not (inner['PodDeviceEntries'] is None or isinstance(inner['PodDeviceEntries'], list))
                or not (inner['RegisteredDevices'] is None or isinstance(inner['RegisteredDevices'], dict))):
            raise RuntimeError('Malformed raw kubelet checkpoint; cannot reconcile allocations')
        value = checkpoint(raw)
        value.update(raw_sha256=hashlib.sha256(text.encode()).hexdigest(), raw_bytes=len(text.encode()),
                     raw_evidence_file=str(raw_file.relative_to(self.out)), stat_before=before, stat_after=after,
                     checkpoint_changed_during_read=before != after)
        save(self.out / 'checkpoints' / (name + '.json'), value)
        if before != after:
            raise RuntimeError('Kubelet checkpoint changed during capture; preserve evidence and retry a new observation')
        if not before or not before.split()[0].isdigit() or int(before.split()[0]) != len(text.encode()):
            raise RuntimeError('Checkpoint stat size and captured raw bytes differ')
        value['verified_r3_raw_uids'] = verify_r3_full_raw_receipts(inner['PodDeviceEntries'] or [])
        save(self.out / 'checkpoints' / (name + '.json'), value)
        return value

    def reconcile_checkpoint(self, name, own_uids=(), deadline=None):
        """Read-only reconciliation; never edit checkpoint, NodeLock or runtime."""
        cp = self.checkpoint(name, deadline)
        def read(args):
            return self.cmd(args, capture_full=True, timeout=checkpoint_read_timeout(deadline)).stdout
        api = json.loads(read(K + ['get', 'pods', '-A', '-o', 'json', '--request-timeout=15s']))
        kubelet = json.loads(read(K + ['get', '--raw=/api/v1/nodes/t3dgq/proxy/pods', '--request-timeout=15s']))
        cri = json.loads(read(['sudo', '-n', 'crictl', 'ps', '-a', '-o', 'json']))
        sandboxes = json.loads(read(['sudo', '-n', 'crictl', 'pods', '-o', 'json']))
        current = json.loads(read(['sudo', '-n', 'crictl', 'ps', '-o', 'json']))
        journal = read(['sudo', '-n', 'journalctl', '-u', 'kubelet', '--since', '30 minutes ago',
                        '-n', '400', '--no-pager', '-o', 'short-iso'])
        for obj, key in ((api, 'items'), (kubelet, 'items'), (cri, 'containers'), (sandboxes, 'items'), (current, 'containers')):
            if not isinstance(obj, dict) or not isinstance(obj.get(key), list):
                raise RuntimeError('Incomplete API/kubelet/CRI observation; refuse checkpoint reconciliation')
        sandbox_uids, ready_uids = {}, set()
        for sandbox in sandboxes['items']:
            uid = sandbox.get('metadata', {}).get('uid')
            if not uid or not sandbox.get('id'):
                raise RuntimeError('CRI sandbox identity is missing')
            sandbox_uids[sandbox['id']] = uid
            if sandbox.get('state') not in ('SANDBOX_NOTREADY', 1):
                ready_uids.add(uid)
        active_cri, unresolved = set(), []
        for container in cri['containers'] + current['containers']:
            uid = container.get('labels', {}).get('io.kubernetes.pod.uid') or sandbox_uids.get(container.get('podSandboxId'))
            if not uid:
                unresolved.append(container.get('id'))
            elif container.get('state') not in ('CONTAINER_EXITED', 2):
                active_cri.add(uid)
        report = classify_checkpoint_receipts(cp['entries'],
            {p['metadata']['uid'] for p in api['items']}, {p['metadata']['uid'] for p in kubelet['items']},
            active_cri, ready_uids, own_uids, cp['verified_r3_raw_uids'])
        report.update(utc=now(), checkpoint=cp, unresolved_cri_container_ids=unresolved,
                      own_pod_uids=sorted(own_uids), native_release_requirement='Existing per-Pod native release checks remain mandatory on the normal cleanup path',
                      abnormal_release_boundary=self.state.get('release_evidence_boundary'),
                      historical_evidence='round8: final parsed summaries and native release logs matched to r2 restore-48; old full raw records not retained. r3 A: complete raw records matched to live proof and restore-607-1; six Pod native releases, UID deletions and API/kubelet/CRI absence independently verified; fresh full-record hashes and workload absence required.')
        if unresolved:
            report['accepted'] = False
        own_set = set(own_uids)
        report['retryable_own_termination'] = bool(
            not report['accepted'] and not unresolved and not report['api_present_uids']
            and set(report['active_relevant_uids']) <= own_set
            and all(row['accepted'] or (row['classification'] == 'active_workload_receipt'
                    and row['pod_uid'] in own_set) for row in report['entries']))
        save(self.out / 'checkpoint-reconciliation' / (name + '.json'), report)
        save(self.out / 'checkpoint-reconciliation' / (name + '-views.json'),
             dict(api_pods=api, kubelet_pods=kubelet, cri_all=cri, cri_current=current, cri_sandboxes=sandboxes,
                  kubelet_recent_journal=journal))
        if not report['accepted']:
            if report['retryable_own_termination']:
                raise OwnWorkloadTerminating('Deleted experiment Pods still present in kubelet/CRI views')
            raise RuntimeError('Unresolved active/unknown checkpoint workload; preserve evidence and stop')
        return report

    def wait_checkpoint_reconciliation(self, name, own_uids):
        """At most 70 seconds for known, API-deleted experiment Pods to exit."""
        deadline, attempt = time.monotonic() + 70, 0
        while True:
            checkpoint_read_timeout(deadline)
            attempt += 1
            try:
                return self.reconcile_checkpoint(name + '-' + str(attempt), own_uids, deadline)
            except OwnWorkloadTerminating:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError('Timed out waiting for experiment workload removal')
                time.sleep(min(2, remaining))

    def proof(self, pod, name):
        from hrrn_stage5_evidence import uuid_evidence
        logs = self.logs(name)
        cp = self.checkpoint(name)
        result = uuid_evidence(pod, logs['scheduler'], logs['rf-model'], cp)
        save(self.out / 'proofs' / (name + '.json'), result)
        # Adapter is finalized against the analyzer's documented result contract.
        if result.get('status') not in ('confirmed', 'verified'):
            raise RuntimeError('RF/Allocate/Device Plugin UUID not proven: ' + json.dumps(result))
        self.state.setdefault('verified_pod_uids', []).append(pod['metadata']['uid'])
        self.save()
        return result

    def submit_job(self, docs):
        results = [self.create(d) for d in docs]
        return [o for o in results if o['kind'] == 'Pod']

    def wait_running(self, pod):
        def running():
            current = self.get('Pod', pod['metadata']['name'], TEST_NS)
            if current['metadata']['uid'] != pod['metadata']['uid']:
                raise RuntimeError('Pod identity changed')
            if current.get('status', {}).get('phase') == 'Failed':
                raise RuntimeError('Experiment Pod failed')
            return current if current.get('status', {}).get('phase') == 'Running' else None
        result = self.wait(running, 90)
        self.wait(lambda: not self.get('node', 't3dgq')['metadata'].get('annotations', {}).get('hamivgpu'), 25)
        return result

    def delete_pod(self, pod):
        uid = pod['metadata']['uid']
        was_bound = bool(pod['spec'].get('nodeName'))
        self.delete(pod_ref(pod))
        self.wait(lambda: not self.get('node', 't3dgq')['metadata'].get('annotations', {}).get('hamivgpu'), 25, check_guard=not self.recovering)
        if was_bound and self.scheduler_pods():
            def released():
                logs = self.logs('release-' + uid)
                return any('rf-job logical release' in line and uid in line for line in logs['scheduler'].splitlines())
            self.wait(released, 20, check_guard=not self.recovering)
        self.event('pod_release_checked', uid=uid, was_bound=was_bound)

    def clear_case(self):
        self.state['active_holder_uids'] = []
        self.state.pop('case_deadline_epoch', None)
        self.save()
        current_pods = []
        for row in list(self.state['created']):
            if row['kind'] == 'Pod':
                p = self.get('Pod', row['name'], row['namespace'], optional=True)
                if p:
                    if p['metadata']['uid'] != row['uid']:
                        raise RuntimeError('Pod UID changed before cleanup')
                    current_pods.append(p)
        # Cancel unbound members before releasing running holders. On failure
        # recovery the experiment scheduler has already stopped (below).
        for p in sorted(current_pods, key=lambda p: bool(p['spec'].get('nodeName'))):
            self.delete_pod(p)
        for row in list(self.state['created']):
            if row['kind'] == 'PodGroup':
                self.delete(row)
        value = self.snapshot('case-clear-' + str(len(self.state['cases'])))
        if value['node_lock'] or any(d['allocated_vgpu_count'] or d['allocated_memory_mib'] or d['allocated_compute_quota'] for d in value['devices']):
            raise RuntimeError('Resources not clear after case')

    def select_mode(self, mode):
        self.scale_experiment(0)
        dep = self.get('deployment', DEPLOYMENT, SYSTEM_NS)
        index = next(i for i, v in enumerate(dep['spec']['template']['spec']['volumes']) if v['name'] == 'config')
        patch = [dict(op='test', path='/metadata/resourceVersion', value=dep['metadata']['resourceVersion']),
                 dict(op='replace', path=f'/spec/template/spec/volumes/{index}/configMap/name', value=self.protocol['configmaps'][mode])]
        self.cmd(K + ['patch', 'deployment', DEPLOYMENT, '-n', SYSTEM_NS, '--type=json', '-p', json.dumps(patch), '--request-timeout=20s'])
        self.scale_experiment(1)

    def order_case(self, case):
        self.select_mode(case['mode'])
        self.state['case_deadline_epoch'] = time.time() + case['scenario_deadline_seconds']
        self.save()
        helpers = []
        for role in case['helper_roles']:
            pod = self.wait_running(self.submit_job(case['jobs'][role])[0])
            self.proof(pod, case['id'] + '-' + role)
            helpers.append(pod)
            self.state['active_holder_uids'] = [p['metadata']['uid'] for p in helpers]
            self.save()
        self.state['active_holder_uids'] = [p['metadata']['uid'] for p in helpers]
        self.save()
        occupied = [allocations(p['metadata'].get('annotations', {}).get('volcano.sh/vgpu-ids-new'))[0]['uuid'] for p in helpers]
        if len(helpers) != 4 or len(set(occupied)) != 4:
            raise RuntimeError('Four holders must occupy four distinct physical GPUs')
        candidates = []
        for role in case['candidate_roles']:
            candidates.extend(self.submit_job(case['jobs'][role]))
            if len(candidates) == 1:
                start = time.monotonic()
                self.wait(lambda: time.monotonic() - start >= case['gap_seconds'], 6)
        # Same process observes all complete memberships while all four GPUs are held.
        observed = self.guard()
        groups = []
        for role in case['candidate_roles']:
            expected_group = next(d for d in case['jobs'][role] if d['kind'] == 'PodGroup')
            group = self.get('PodGroup', expected_group['metadata']['name'], TEST_NS)
            known_group = next(o for o in self.state['created'] if o['kind'] == 'PodGroup' and o['name'] == group['metadata']['name'])
            expected = {p['metadata']['uid'] for p in candidates if p['metadata']['annotations']['scheduling.k8s.io/group-name'] == group['metadata']['name']}
            members = {p['metadata']['uid'] for p in observed if p['metadata']['namespace'] == TEST_NS
                       and p['metadata'].get('annotations', {}).get('scheduling.k8s.io/group-name') == group['metadata']['name']}
            if group['metadata']['uid'] != known_group['uid'] or members != expected or group['spec'].get('queue') != self.protocol['queue_name']:
                raise RuntimeError('API complete membership/queue differs from submitted identities')
            groups.append(group)
            self.state.setdefault('job_observations', {})[TEST_NS + '/' + group['metadata']['name']] = dict(complete_ready_observed_at=now())
        self.save()
        self.event('all_candidates_externally_observed', case=case['id'], pod_uids=[p['metadata']['uid'] for p in candidates], podgroup_uids=[g['metadata']['uid'] for g in groups])
        start = time.monotonic()
        self.wait(lambda: time.monotonic() - start >= case['min_wait_seconds'], 40)
        if case['mode'] != 'A':
            from hrrn_stage5_evidence import latest_waiting
            keys = [TEST_NS + '/' + g['metadata']['name'] for g in groups]
            def tracker_ready():
                rows = latest_waiting(self.logs(case['id'] + '-before-release')['scheduler'], keys)
                return all(rows[k] and rows[k]['ready'] and rows[k]['waiting_seconds'] >= case['min_wait_seconds'] for k in keys)
            self.wait(tracker_ready, 35)
        else:
            self.logs(case['id'] + '-before-release')
        for pod in candidates:
            current = self.get('Pod', pod['metadata']['name'], TEST_NS)
            if current['spec'].get('nodeName'):
                raise RuntimeError('Candidate unexpectedly bound before controlled release')
        gate_name = case['jobs'][case['gate_role']][1]['metadata']['name']
        gate = next(p for p in helpers if p['metadata']['name'] == gate_name)
        self.event('competition_resource_release', case=case['id'], holder_uid=gate['metadata']['uid'])
        self.state['active_holder_uids'].remove(gate['metadata']['uid'])
        self.save()
        self.delete_pod(gate)
        first = None
        def first_bound():
            nonlocal first
            current = [self.get('Pod', p['metadata']['name'], TEST_NS) for p in candidates]
            selected = [p for p in current if p['spec'].get('nodeName')]
            if len(selected) > 1:
                raise RuntimeError('Missed unambiguous first live binding observation; two historical nodeName values do not prove concurrency')
            if selected:
                first = selected[0]
                return True
            return False
        self.wait(first_bound, 60)
        first = self.wait_running(first)
        first_proof = self.proof(first, case['id'] + '-first')
        from hrrn_stage5_evidence import comparison_evidence
        allocation = first_proof.get('selected_native_allocation')
        if not allocation or allocation.get('session') is None:
            raise RuntimeError('Actual consumed UUID could not be linked to one allocating Session')
        log = (self.out / 'logs' / (case['id'] + '-first-scheduler.log')).read_text()
        pair = [TEST_NS + '/' + g['metadata']['name'] for g in groups]
        comparison = comparison_evidence(log, pair[0], pair[1], session=allocation['session'], before_line=allocation['line_number'])
        save(self.out / 'cases' / (case['id'] + '-comparison.json'), comparison)
        priorities = {self.get('Pod', p['metadata']['name'], TEST_NS)['spec'].get('priority', 0) for p in candidates}
        if len(priorities) != 1 or not comparison['all_three_preceding_plugins_have_tie_evidence'] or comparison['any_preceding_non_tie']:
            raise RuntimeError('Priority/gang/DRF equality in the decisive Session is not proven')
        decisive_hrrn = None
        if case['mode'] == 'B':
            decisive_hrrn = validate_decisive_hrrn(comparison, first_proof, first, pair[0], pair[1], case['min_wait_seconds'])
            save(self.out / 'cases' / (case['id'] + '-decisive-hrrn.json'), decisive_hrrn)
        expected_role = case['expected_initial_bind']
        expected_name = next(d['metadata']['name'] for d in case['jobs'][expected_role] if d['kind'] == 'Pod')
        row = dict(case=case['id'], mode=case['mode'], first_pod=first['metadata']['name'], expected_first_pod=expected_name,
                   ordering_matched=first['metadata']['name'] == expected_name, real_wait_used=True,
                   ready_time_source='external_API_complete_membership' if case['mode'] == 'A' else 'real_HRRN_tracker',
                   waiting_time_boundary=('A records elapsed time after external API complete-membership observation; native HRRN Tracker data is unavailable'
                                          if case['mode'] == 'A' else 'B validates both ready snapshots in the final consumed allocation Session'),
                   decisive_session=allocation['session'], comparison=comparison, decisive_hrrn=decisive_hrrn,
                   hrrn_snapshot_at_allocation=first_proof.get('hrrn_snapshot_at_allocation'))
        # Finite sleep completion is observed; no synthetic scheduler clock.
        for original in sorted(candidates, key=lambda p: p['metadata']['name'] != first['metadata']['name']):
            def completed():
                p = self.get('Pod', original['metadata']['name'], TEST_NS)
                if p.get('status', {}).get('phase') == 'Running':
                    return p
                if p.get('status', {}).get('phase') == 'Succeeded':
                    return p
                if p.get('status', {}).get('phase') == 'Failed':
                    raise RuntimeError('Candidate failed')
                return None
            pod = self.wait(completed, 100)
            if pod['metadata']['uid'] not in self.state.get('verified_pod_uids', []):
                if pod.get('status', {}).get('phase') != 'Running':
                    raise RuntimeError('Pod completed before live Device Plugin UUID proof was captured')
                self.proof(pod, case['id'] + '-' + pod['metadata']['name'])
            self.wait(lambda: self.get('Pod', original['metadata']['name'], TEST_NS).get('status', {}).get('phase') == 'Succeeded', 65)
        final_logs = self.logs(case['id'] + '-complete')
        final_pods = [self.get('Pod', p['metadata']['name'], TEST_NS) for p in candidates]
        from hrrn_stage5_evidence import extract_timing
        observations = {'jobs': self.state.get('job_observations', {}), 'pods': {}}
        history = self.out / 'pod-observations.jsonl'
        if history.exists():
            for line in history.read_text().splitlines():
                event = json.loads(line)
                if event.get('node'):
                    observations['pods'].setdefault(event['uid'], {}).setdefault('first_bound_at', event['utc'])
        save(self.out / 'cases' / (case['id'] + '-final-pods.json'), {'items': final_pods})
        save(self.out / 'cases' / (case['id'] + '-groups.json'), {'items': groups})
        timings = extract_timing(final_pods, groups, final_logs['scheduler'], observations)
        save(self.out / 'cases' / (case['id'] + '-timings.json'), timings)
        events = json.loads(self.cmd(K + ['get', 'events', '-n', TEST_NS, '-o', 'json', '--request-timeout=15s']).stdout)
        save(self.out / 'cases' / (case['id'] + '-events.json'), events)
        self.state['cases'].append(row)
        self.save()
        self.clear_case()
        if not row['ordering_matched']:
            raise RuntimeError('Observed ordering differs; preserved as failure, do not claim HRRN success')

    def recover(self):
        if self.state.get('restored'):
            return
        self.recovering = True
        self.state['phase'] = 'recovering'
        self.save()
        self.adopt_pending_creates()
        if (self.state.get('foreign_conflict') or self.state.get('error') or self.state.get('abnormal_recovery')) and self.scheduler_pods():
            try:
                self.logs('failure-before-stop')
            except Exception as exc:
                self.event('failure_log_capture_failed', error=repr(exc))
            self.scale_experiment(0)
            self.state['release_evidence_boundary'] = 'Experiment process stopped before failure cleanup; native per-Pod release logs may be unavailable. Require API/lock reconciliation; never delete foreign workloads.'
            self.save()
        self.clear_case()
        own_deps = [o for o in self.state['created'] if o['kind'] == 'Deployment']
        if own_deps and self.get('deployment', DEPLOYMENT, SYSTEM_NS, optional=True):
            if self.scheduler_pods():
                self.logs('final-before-stop')
            self.scale_experiment(0)
        if self.scheduler_pods():
            raise RuntimeError('Experiment scheduler still exists; do not restart a competing scheduler')
        own_pod_uids = {o['uid'] for o in self.state['created'] if o['kind'] == 'Pod'}
        # Disk receipts can outlive kubelet's active allocation map. Reconcile
        # identities and independent API/kubelet/CRI evidence, never erase them.
        self.wait_checkpoint_reconciliation('restore-' + str(self.counter), own_pod_uids)
        original, quota = self.prior('original'), self.prior('quota')
        current = self.get('resourcequota', 'rf-v7-test-budget', TEST_NS)
        peak = {**quota['spec'], 'hard': self.protocol['peak_quota']}
        if current['metadata']['uid'] != quota['metadata']['uid'] or current['spec'] not in (quota['spec'], peak):
            raise RuntimeError('Quota identity/spec changed externally')
        if current['spec'] != quota['spec']:
            self.wait(lambda: all(str(v) == '0' for v in self.get('resourcequota', 'rf-v7-test-budget', TEST_NS).get('status', {}).get('used', {}).values()), 35, check_guard=False)
            current = self.get('resourcequota', 'rf-v7-test-budget', TEST_NS)
            patch = [dict(op='test', path='/metadata/resourceVersion', value=current['metadata']['resourceVersion']),
                     dict(op='replace', path='/spec', value=quota['spec'])]
            self.cmd(K + ['patch', 'resourcequota', 'rf-v7-test-budget', '-n', TEST_NS, '--type=json', '-p', json.dumps(patch), '--request-timeout=20s'])
        current = self.get('deployment', 'volcano-scheduler', 'volcano-system')
        if current['metadata']['uid'] != original['metadata']['uid'] or current['spec'] not in (original['spec'], {**original['spec'], 'replicas': 0}):
            raise RuntimeError('Original Deployment changed externally; refuse overwrite')
        self.patch_replicas('volcano-scheduler', 'volcano-system', original['spec']['replicas'], original['metadata']['uid'])
        self.cmd(K + ['rollout', 'status', 'deployment/volcano-scheduler', '-n', 'volcano-system', '--timeout=90s'], timeout=100)
        self.process_sha('volcano-scheduler', 'volcano-system', ORIGINAL_SHA)
        for row in list(reversed(self.state['created'])):
            if row['kind'] in ('Deployment', 'ConfigMap', 'Lease'):
                self.delete(row)
        checks = {}
        for key, kind, name, ns in [('original', 'deployment', 'volcano-scheduler', 'volcano-system'), ('rf', 'deployment', 'volcano-rf-test', SYSTEM_NS), ('dqn', 'deployment', 'dqn-scheduler-grpc', 'volcano-system'), ('quota', 'resourcequota', 'rf-v7-test-budget', TEST_NS)]:
            obj, prior = self.get(kind, name, ns), self.prior(key)
            checks[key] = obj['metadata']['uid'] == prior['metadata']['uid'] and obj['spec'] == prior['spec']
        config, prior_config = self.get('configmap', 'volcano-scheduler-configmap', 'volcano-system'), self.prior('original-config')
        checks['original_config'] = config['metadata']['uid'] == prior_config['metadata']['uid'] and config['data'] == prior_config['data']
        for key, name in [('rf-config', 'rf-v7-config-on'), ('geometry', 'rf-v7-device-config')]:
            obj, prior = self.get('configmap', name, SYSTEM_NS), self.prior(key)
            checks[key] = obj['metadata']['uid'] == prior['metadata']['uid'] and obj['data'] == prior['data']
        dqn = self.get('deployment', 'dqn-scheduler-grpc', 'volcano-system')
        checks['dqn_available'] = dqn.get('status', {}).get('availableReplicas', 0) == self.prior('dqn')['spec']['replicas']
        model = '/home/zbs/rf-round7-live-window-20261019/lab/models/pod-gpu-rf-v1/random_forest.joblib'
        checks['model_sha'] = self.cmd(['sha256sum', model]).stdout.split()[0] == MODEL_SHA
        if not all(checks.values()):
            raise RuntimeError('Restoration comparison failed: ' + str(checks))
        self.state.update(restored=True, restored_utc=now(), phase='restored')
        self.save()
        save(self.out / 'restore-confirmation.json', dict(utc=now(), checks=checks, original_process_sha=ORIGINAL_SHA,
                                                        cluster_audit_retained=True, foreign_workloads_deleted=False))

    def ensure_immutable_image(self, image):
        """Add only this frozen image's missing repository digest alias.

        OCI import --digests need not create a repository-qualified CRI alias.
        The explicit local tag path uses ImageService.Create, without force or
        replacement; every accepted existing/concurrently created alias must
        resolve to the frozen manifest and configuration before proceeding.
        """
        source = 'docker.io/gpu-lab/volcano-hrrn:stage5-dbedbd61'
        manifest_digest = 'sha256:c03dae667ffc821f6ada20373e00434f1ec8d3f06a1c8c9ebba5c0759bef7947'
        config_digest = 'sha256:528d21f63c21e7a6804daa145df47f50544f6e20c1c897ff52f88c95f32b724a'
        target = 'docker.io/gpu-lab/volcano-hrrn@' + manifest_digest
        expected = dict(image_ref=source, immutable_ref=target, manifest_digest=manifest_digest,
                        config_digest=config_digest, binary_sha256=HRRN_SHA)
        if any(image.get(key) != value for key, value in expected.items()):
            raise RuntimeError('Image alias request is outside the exact frozen Stage5 image')
        ctr = ['sudo', '-n', 'ctr', '-n', 'k8s.io']

        def checked_listing(require_target=False):
            listing = self.cmd(ctr + ['images', 'list'], capture_full=True).stdout
            found = {}
            for line in listing.splitlines():
                fields = line.split()
                if not fields or fields[0] not in (source, target):
                    continue
                if fields[0] in found or len(fields) < 3:
                    raise RuntimeError('Ambiguous frozen image reference in containerd listing')
                if fields[1] != 'application/vnd.oci.image.manifest.v1+json' or fields[2] != manifest_digest:
                    raise RuntimeError('Refuse image alias: existing source/target manifest differs')
                found[fields[0]] = fields[2]
            if source not in found or (require_target and target not in found):
                raise RuntimeError('Frozen image source/target reference is missing from containerd')
            return found

        before = checked_listing()
        blobs = {}
        for value in (manifest_digest, config_digest):
            raw = self.cmd(ctr + ['content', 'get', value], capture_full=True).stdout
            if 'sha256:' + hashlib.sha256(raw.encode()).hexdigest() != value:
                raise RuntimeError('Frozen containerd content digest mismatch')
            blobs[value] = json.loads(raw)
        if blobs[manifest_digest].get('config', {}).get('digest') != config_digest:
            raise RuntimeError('Frozen manifest does not reference the expected configuration')
        config = blobs[config_digest]
        if (config.get('os'), config.get('architecture')) != ('linux', 'amd64') or config.get('config', {}).get('Labels', {}).get('gpu-lab.scheduler-binary-sha256') != HRRN_SHA:
            raise RuntimeError('Frozen image configuration does not identify the Stage4 binary')

        def cri_ready(ref, require_repo_digest=False):
            result = self.cmd(['sudo', '-n', 'crictl', 'inspecti', '-o', 'json', ref],
                              check=False, timeout=10, capture_full=True)
            if result.returncode:
                return None  # A bounded read-only wait permits CRI event propagation.
            status = json.loads(result.stdout).get('status', {})
            if status.get('id') != config_digest:
                raise RuntimeError('Refuse image alias: CRI configuration ID differs')
            if ref == source and source not in status.get('repoTags', []):
                raise RuntimeError('CRI source lookup did not resolve the exact frozen tag')
            if require_repo_digest and target not in status.get('repoDigests', []):
                return None
            return status

        source_status = self.wait(lambda: cri_ready(source), 15)
        created = target not in before
        tag_exit = None
        if created:
            self.guard()
            # Recheck immediately before the only mutation. --local without
            # --force refuses an existing target instead of replacing it.
            if target not in checked_listing():
                self.event('immutable_image_alias_intent', source=source, target=target,
                           manifest_digest=manifest_digest, config_digest=config_digest)
                tagged = self.cmd(ctr + ['images', 'tag', '--local', source, target],
                                  check=False, capture_full=True)
                tag_exit = tagged.returncode
            else:
                created = False
        after = checked_listing(require_target=True)
        target_status = self.wait(lambda: cri_ready(target, require_repo_digest=True), 15)
        proof = dict(utc=now(), source=source, target=target, manifest_digest=manifest_digest,
                     config_digest=config_digest, source_cri=source_status, target_cri=target_status,
                     references_before=before, references_after=after, alias_requested=created,
                     tag_exit_code=tag_exit, force_used=False, existing_images_deleted=False)
        save(self.out / 'image-alias-proof.json', proof)
        self.event('immutable_image_alias_verified', target=target, config_digest=config_digest,
                   alias_requested=created, tag_exit_code=tag_exit)
        return proof

    def execute(self):
        if self.state['phase'] != 'prepared':
            raise RuntimeError('Prepare first; attempts are not repeatable')
        cases = validate_execution_scope(self.protocol['cases'])
        self.state.update(started_epoch=time.time(), phase='executing')
        self.state['runner_identity'] = process_identity(os.getpid())
        self.save()
        with (self.out / 'watchdog.log').open('a') as log:
            watcher = subprocess.Popen([sys.executable, str(Path(__file__)), '--watchdog', str(os.getpid())],
                                       stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        self.state['watchdog_pid'] = watcher.pid
        self.save()
        try:
            self.guard()
            gpu = self.cmd(['nvidia-smi']).stdout
            if 'No running processes found' not in gpu:
                raise RuntimeError('GPU process appeared since prepare')
            for key, kind, name, ns in [('original', 'deployment', 'volcano-scheduler', 'volcano-system'), ('rf', 'deployment', 'volcano-rf-test', SYSTEM_NS), ('dqn', 'deployment', 'dqn-scheduler-grpc', 'volcano-system')]:
                if self.get(kind, name, ns)['spec'] != self.prior(key)['spec']:
                    raise RuntimeError('Deployment changed after prepare')
            image = json.loads((self.root / 'image.json').read_text())
            archive = self.root / 'scheduler.oci.tar'
            if hashlib.sha256(archive.read_bytes()).hexdigest() != image['archive_sha256']:
                raise RuntimeError('Immutable image archive corrupted')
            self.cmd(['sudo', '-n', 'ctr', '-n', 'k8s.io', 'images', 'import', '--digests', str(archive)], timeout=90, capture_full=True)
            self.ensure_immutable_image(image)
            for obj in self.protocol['setup_objects']:
                self.create(obj)
            lease = self.create(dict(apiVersion='coordination.k8s.io/v1', kind='Lease',
                metadata=dict(name=DEPLOYMENT, namespace=SYSTEM_NS,
                              labels={'gpu-lab/experiment': 'hrrn-stage5', 'gpu-lab/run-id': self.run_id}), spec={}))
            self.state['lease_uid'] = lease['metadata']['uid']
            self.save()
            quota = self.get('resourcequota', 'rf-v7-test-budget', TEST_NS)
            if quota['spec'] != self.prior('quota')['spec']:
                raise RuntimeError('Quota changed after prepare')
            patch = [dict(op='test', path='/metadata/resourceVersion', value=quota['metadata']['resourceVersion']),
                     dict(op='replace', path='/spec/hard', value=self.protocol['peak_quota'])]
            self.cmd(K + ['patch', 'resourcequota', 'rf-v7-test-budget', '-n', TEST_NS, '--type=json', '-p', json.dumps(patch), '--request-timeout=20s'])
            self.state['old_scale_intent'] = True
            self.save()
            original = self.prior('original')
            self.patch_replicas('volcano-scheduler', 'volcano-system', 0, original['metadata']['uid'])
            self.wait(lambda: not any(p['metadata']['namespace'] == 'volcano-system' and p['metadata']['name'].startswith('volcano-scheduler-') for p in self.get('pods')['items']), 65, check_guard=False)
            running = json.loads(self.cmd(['sudo', '-n', 'crictl', 'ps', '-o', 'json']).stdout)['containers']
            if any(c.get('metadata', {}).get('name') == 'volcano-scheduler' for c in running):
                raise RuntimeError('Original scheduler process has not exited')
            self.state['experiment_started'] = True
            self.save()
            clean = self.snapshot('original-exited')
            if clean['node_lock'] or any(d['allocated_vgpu_count'] for d in clean['devices']):
                raise RuntimeError('In-flight allocations after original process exited')
            for case in cases:
                # A/B sorting is the approved first live tranche. C stays blocked.
                if case.get('execution_enabled') and case['kind'] == 'order':
                    self.order_case(case)
            self.state['experiment_success'] = all(c['ordering_matched'] for c in self.state['cases']) and len(self.state['cases']) == 2
            self.save()
        except BaseException as exc:
            self.state.update(experiment_success=False, error=repr(exc))
            self.save()
            self.event('experiment_error', error=repr(exc))
        finally:
            try:
                self.recover()
            except BaseException as exc:
                self.state.update(phase='recovery_needs_attention', recovery_error=repr(exc))
                self.save()
                self.event('recovery_needs_attention', error=repr(exc))
                raise


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--prepare', action='store_true')
    ap.add_argument('--execute', action='store_true')
    ap.add_argument('--recover', action='store_true')
    ap.add_argument('--watchdog', type=int)
    args = ap.parse_args()
    if args.watchdog:
        started = time.monotonic()
        while time.monotonic() - started < WINDOW_SECONDS + 120:
            time.sleep(5)
            r = Run()
            if r.state.get('restored'):
                return
            if process_identity(args.watchdog) != r.state.get('runner_identity'):
                break
            hb = r.out / 'heartbeat'
            if hb.exists() and time.time() - hb.stat().st_mtime > 140:
                break
        (ROOT / 'STOP').touch()
        expected = Run().state.get('runner_identity')
        if not expected or expected['pid'] != args.watchdog:
            raise RuntimeError('Missing durable worker process identity')
        if process_identity(args.watchdog) == expected:
            signal_owned_worker(expected, signal.SIGTERM, Path(__file__))
        with (ROOT / 'run.lock').open('a') as lock:
            try:
                acquire_recovery_lock(lock, 150)
            except TimeoutError:
                if process_identity(args.watchdog) == expected:
                    signal_owned_worker(expected, signal.SIGKILL, Path(__file__))
                acquire_recovery_lock(lock, 30)
            recovery = Run()
            if not recovery.state.get('restored'):
                recovery.state['abnormal_recovery'] = True
                recovery.save()
                recovery.recover()
        return
    with (ROOT / 'run.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        def interrupted(sig, frame):
            raise RuntimeError('Interrupted: ' + str(sig))
        signal.signal(signal.SIGTERM, interrupted)
        signal.signal(signal.SIGINT, interrupted)
        run = Run()
        if args.prepare:
            run.prepare()
        elif args.execute:
            run.execute()
        elif args.recover:
            if not run.state.get('restored'):
                run.state['abnormal_recovery'] = True
                run.save()
                run.recover()
        else:
            ap.error('Choose prepare, execute, or recover')


if __name__ == '__main__':
    main()
