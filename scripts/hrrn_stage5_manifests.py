"""Pure, local manifests and protocol for the bounded HRRN Stage5 experiment.

This module never contacts Kubernetes. ConfigMap text uses JSON, a valid YAML
subset accepted by Volcano's scheduler configuration reader. All candidate
durations are synthetic sleep durations, not GPU performance measurements.
"""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import re

SYSTEM_NS = 'gpu-rf-system'
TEST_NS = 'gpu-rf-tests'
SCHEDULER_NAME = 'volcano-hrrn-stage5'
SERVICE_ACCOUNT = 'volcano-rf-test'
QUEUE_NAME = 'rf-v7-queue'
NODE_NAME = 't3dgq'
DEVICE_CONFIG = 'rf-v7-device-config'
EXPERIMENT = 'hrrn-stage5'
SCHEDULER_IMAGE = 'docker.io/gpu-lab/volcano-hrrn@sha256:c03dae667ffc821f6ada20373e00434f1ec8d3f06a1c8c9ebba5c0759bef7947'
RF_IMAGE = 'docker.io/gpu-lab/rf-model@sha256:7ef2fd82031578388f75e101f9a6ab572faacf7c96fa27e39bfcfe8b213c4259'
SLEEP_IMAGE = 'docker.io/pytorch/pytorch@sha256:923f687790bec78081c357e71dcd5dcef80b0cc00f6c34484902a5e83362c854'
MODEL_VERSION = 'rf-v4-known-states-ee35cfabe30f'
ESTIMATE = 'gpu-lab/estimated-duration-seconds'
TOTAL_PODS = 'gpu-lab/total-pods'
RUN_LABEL = 'gpu-lab/run-id'
HELPER_SLEEP = 300
CANDIDATE_SLEEP = 30
MEMORY_MIB = 4096
PEAK_QUOTA = {
    'pods': '8', 'requests.volcano.sh/vgpu-number': '8',
    'requests.volcano.sh/vgpu-memory': '32768',
    'requests.volcano.sh/vgpu-cores': '600',
}
CASES = ('order-a', 'order-b', 'complete-a', 'complete-c')


def _token(value, what, limit=20):
    if not isinstance(value, str) or len(value) > limit or not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]*[a-z0-9])?', value):
        raise ValueError(f'{what} must be a lower-case DNS label of 1..{limit} characters')
    return value


def _integer(value, what, lower, upper):
    if type(value) is not int or not lower <= value <= upper:
        raise ValueError(f'{what} must be an integer in [{lower}, {upper}]')
    return value


def _labels(run_id, case=None, role=None):
    result = {'gpu-lab/experiment': EXPERIMENT, RUN_LABEL: _token(run_id, 'run_id')}
    if case is not None:
        result['gpu-lab/case'] = _token(case, 'case')
    if role is not None:
        result['gpu-lab/role'] = _token(role, 'role')
    return result


def object_name(run_id, case, role):
    name = f'hrrn-s5-{_token(run_id, "run_id")}-{_token(case, "case")}-{_token(role, "role")}'
    if len(name) > 59:  # Reserve the member suffix and stay within 63 characters.
        raise ValueError('Generated workload name is too long')
    return name


def configmap_name(run_id, mode):
    if mode not in ('A', 'B', 'C'):
        raise ValueError('Mode must be A, B, or C')
    return f'hrrn-s5-{_token(run_id, "run_id")}-{mode.lower()}-rf'


def configs():
    """A/B/C differ only in enabled and complete-job HRRN arguments."""
    base = {
        'actions': 'enqueue, allocate, backfill',
        'tiers': [
            {'plugins': [{'name': 'priority'}, {'name': 'gang', 'enablePreemptable': False}, {'name': 'conformance'}]},
            {'plugins': [
                {'name': 'overcommit'}, {'name': 'drf', 'enablePreemptable': False},
                {'name': 'predicates', 'arguments': {'predicate.CacheEnable': False}},
                {'name': 'proportion'}, {'name': 'nodeorder'}, {'name': 'binpack'},
                {'name': 'deviceshare', 'arguments': {
                    'deviceshare.VGPUEnable': True, 'deviceshare.GPUSharingEnable': False,
                    'deviceshare.GPUNumberEnable': False, 'deviceshare.NodeLockEnable': True,
                    'deviceshare.SchedulePolicy': 'binpack', 'deviceshare.ScheduleWeight': 1,
                    'deviceshare.GPUSelectPolicy': 'rf-job', 'deviceshare.RFEnable': True,
                    'deviceshare.RFPredictionEnabled': True,
                    'deviceshare.RFEndpoint': 'http://127.0.0.1:18081/v1/rank',
                    'deviceshare.RFModelVersion': MODEL_VERSION, 'deviceshare.RFTimeoutMS': 1000,
                    'deviceshare.KnownGeometriesCMName': DEVICE_CONFIG,
                    'deviceshare.KnownGeometriesCMNamespace': SYSTEM_NS,
                }},
                {'name': 'hrrn', 'enableJobOrder': True, 'enableJobReady': True,
                 'arguments': {'enabled': False, 'complete-job': False,
                               'tau-seconds': 1.0, 'default-duration-seconds': 60.0}},
            ]},
        ],
    }
    result = {}
    for mode, enabled, complete in [('A', False, False), ('B', True, False), ('C', True, True)]:
        config = copy.deepcopy(base)
        config['tiers'][1]['plugins'][-1]['arguments'].update({'enabled': enabled, 'complete-job': complete})
        result[mode] = config
    return result


def configmaps(run_id):
    return [dict(apiVersion='v1', kind='ConfigMap',
                 metadata=dict(name=configmap_name(run_id, mode), namespace=SYSTEM_NS, labels=_labels(run_id)),
                 data={'volcano-scheduler.conf': json.dumps(config, indent=2) + '\n'})
            for mode, config in configs().items()]


def scheduler_deployment(run_id, mode='A'):
    labels = {**_labels(run_id), 'app': SCHEDULER_NAME}
    container_security = {'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True,
                          'capabilities': {'drop': ['ALL']}}
    health = ['python', '-c', "import json,urllib.request; r=json.load(urllib.request.urlopen('http://127.0.0.1:18081/health',timeout=1)); assert r['model_ready'] and r['model_version']=='" + MODEL_VERSION + "'"]
    result = {
        'apiVersion': 'apps/v1', 'kind': 'Deployment',
        'metadata': {'name': SCHEDULER_NAME, 'namespace': SYSTEM_NS, 'labels': labels},
        'spec': {'replicas': 0, 'strategy': {'type': 'Recreate'}, 'revisionHistoryLimit': 1,
                 'selector': {'matchLabels': {'app': SCHEDULER_NAME, RUN_LABEL: run_id}},
                 'template': {'metadata': {'labels': labels}, 'spec': {
                     'serviceAccountName': SERVICE_ACCOUNT, 'automountServiceAccountToken': False,
                     'nodeSelector': {'kubernetes.io/hostname': NODE_NAME},
                     'terminationGracePeriodSeconds': 10,
                     'securityContext': {'runAsNonRoot': True, 'runAsUser': 1000, 'runAsGroup': 1000,
                                         'fsGroup': 1000, 'seccompProfile': {'type': 'RuntimeDefault'}},
                     'containers': [
                         {'name': 'scheduler', 'image': SCHEDULER_IMAGE, 'imagePullPolicy': 'Never',
                          'command': ['/vc-scheduler-hrrn'], 'args': [
                              '--scheduler-name=' + SCHEDULER_NAME, '--scheduler-conf=/rf-config/volcano-scheduler.conf',
                              '--default-queue=' + QUEUE_NAME, '--node-selector=kubernetes.io/hostname:' + NODE_NAME,
                              '--leader-elect=true', '--leader-elect-resource-name=' + SCHEDULER_NAME,
                              '--leader-elect-resource-namespace=' + SYSTEM_NS, '--leader-elect-resource-lock=leases',
                              '--enable-healthz=true', '--healthz-address=:18091', '--enable-metrics=true',
                              '--listen-address=:18090', '--cache-dumper=false', '--logtostderr', '--v=4'],
                          'securityContext': copy.deepcopy(container_security),
                          'resources': {'requests': {'cpu': '100m', 'memory': '256Mi'}, 'limits': {'cpu': '2', 'memory': '1Gi'}},
                          'ports': [{'name': 'metrics', 'containerPort': 18090}, {'name': 'health', 'containerPort': 18091}],
                          'readinessProbe': {'httpGet': {'path': '/healthz', 'port': 'health'}, 'periodSeconds': 3},
                          'volumeMounts': [{'name': 'config', 'mountPath': '/rf-config', 'readOnly': True},
                                           {'name': 'tmp', 'mountPath': '/tmp'},
                                           {'name': 'api-token', 'mountPath': '/var/run/secrets/kubernetes.io/serviceaccount', 'readOnly': True}]},
                         {'name': 'rf-model', 'image': RF_IMAGE, 'imagePullPolicy': 'Never',
                          'command': ['python', '-m', 'gpu_lab.runtime.audit_server', '--port', '18081'],
                          'env': [{'name': name, 'value': '1'} for name in ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS')],
                          'securityContext': copy.deepcopy(container_security),
                          'resources': {'requests': {'cpu': '100m', 'memory': '256Mi'}, 'limits': {'cpu': '1', 'memory': '1Gi'}},
                          'readinessProbe': {'exec': {'command': health}, 'periodSeconds': 3},
                          'startupProbe': {'exec': {'command': health}, 'periodSeconds': 2, 'failureThreshold': 30},
                          'volumeMounts': [{'name': 'rf-tmp', 'mountPath': '/tmp'}]},
                     ],
                     'volumes': [{'name': 'config', 'configMap': {'name': configmap_name(run_id, mode)}},
                                 {'name': 'tmp', 'emptyDir': {}}, {'name': 'rf-tmp', 'emptyDir': {}},
                                 {'name': 'api-token', 'projected': {'sources': [
                                     {'serviceAccountToken': {'path': 'token', 'expirationSeconds': 3600}},
                                     {'configMap': {'name': 'kube-root-ca.crt', 'items': [{'key': 'ca.crt', 'path': 'ca.crt'}]}},
                                     {'downwardAPI': {'items': [{'path': 'namespace', 'fieldRef': {'fieldPath': 'metadata.namespace'}}]}},
                                 ]}}],
                 }}},
    }
    validate_manifest(result)
    return result


def _pod_spec(cores, sleep_seconds):
    _integer(sleep_seconds, 'sleep_seconds', 1, HELPER_SLEEP)
    requests, limits = {'cpu': '100m', 'memory': '128Mi'}, {'cpu': '500m', 'memory': '256Mi'}
    if cores:
        _integer(cores, 'cores per Pod', 1, 100)
        gpu = {'volcano.sh/vgpu-number': '1', 'volcano.sh/vgpu-memory': str(MEMORY_MIB),
               'volcano.sh/vgpu-cores': str(cores)}
        requests.update(gpu)
        limits.update(gpu)
    spec = {
        'schedulerName': SCHEDULER_NAME, 'restartPolicy': 'Never',
        'automountServiceAccountToken': False, 'activeDeadlineSeconds': sleep_seconds + 180,
        'terminationGracePeriodSeconds': 5,
        'nodeSelector': {'kubernetes.io/hostname': NODE_NAME},
        'tolerations': [{'key': 'node-role.kubernetes.io/' + key, 'operator': 'Exists', 'effect': 'NoSchedule'}
                        for key in ('control-plane', 'master')],
        'containers': [{'name': 'sleep', 'image': SLEEP_IMAGE, 'imagePullPolicy': 'Never',
                        'command': ['/bin/sleep', str(sleep_seconds)],
                        'resources': {'requests': requests, 'limits': limits},
                        'securityContext': {'allowPrivilegeEscalation': False, 'capabilities': {'drop': ['ALL']}}}],
    }
    if cores:
        spec['runtimeClassName'] = 'nvidia'
    return spec


def standalone_job(run_id, case, name, cores, estimate, sleep_seconds, pods=1, min_available=None):
    """A complete submitted membership: [PodGroup, Pod ...], without forged time."""
    _integer(cores, 'cores per Pod', 1, 100)
    _integer(estimate, 'estimated duration', 1, 3600)
    _integer(pods, 'pods', 1, 2)
    minimum = pods if min_available is None else _integer(min_available, 'min_available', 1, pods)
    group = object_name(run_id, case, name)
    labels = {**_labels(run_id, case, name), 'gpu-lab/workload-type': 'training'}
    objects = [{
        'apiVersion': 'scheduling.volcano.sh/v1beta1', 'kind': 'PodGroup',
        'metadata': {'name': group, 'namespace': TEST_NS, 'labels': labels,
                     'annotations': {ESTIMATE: str(estimate), TOTAL_PODS: str(pods)}},
        # minResources intentionally absent: enqueue must not prevent observing
        # precreated members while the four GPUs are held by helper Pods.
        'spec': {'minMember': minimum, 'queue': QUEUE_NAME},
    }]
    for index in range(pods):
        objects.append({'apiVersion': 'v1', 'kind': 'Pod',
                        'metadata': {'name': group + '-' + str(index), 'namespace': TEST_NS, 'labels': labels,
                                     'annotations': {'scheduling.k8s.io/group-name': group}},
                        'spec': _pod_spec(cores, sleep_seconds)})
    for obj in objects:
        validate_manifest(obj)
    return objects


def propagation_job(run_id):
    """Separate real VolcanoJob metadata propagation check; no GPU requested."""
    result = {'apiVersion': 'batch.volcano.sh/v1alpha1', 'kind': 'Job',
              'metadata': {'name': object_name(run_id, 'metadata', 'propagation'), 'namespace': TEST_NS,
                           'labels': _labels(run_id, 'metadata', 'propagation'),
                           'annotations': {ESTIMATE: '37', TOTAL_PODS: '1'}},
              'spec': {'schedulerName': SCHEDULER_NAME, 'queue': QUEUE_NAME, 'minAvailable': 1, 'maxRetry': 0,
                       'tasks': [{'name': 'worker', 'replicas': 1, 'minAvailable': 1,
                                  'template': {'metadata': {'labels': _labels(run_id, 'metadata', 'propagation')},
                                               'spec': _pod_spec(0, 5)}}]}}
    validate_manifest(result)
    return result


def scenario_jobs(run_id, case):
    if case not in CASES:
        raise ValueError('Unknown scenario')
    jobs = {f'blocker-{index}': standalone_job(run_id, case, f'blocker-{index}', 100, 300, HELPER_SLEEP)
            for index in range(3)}
    if case.startswith('order-'):
        jobs['hold'] = standalone_job(run_id, case, 'hold', 100, 300, HELPER_SLEEP)
        jobs['long'] = standalone_job(run_id, case, 'long', 100, 100, CANDIDATE_SLEEP)
        jobs['short'] = standalone_job(run_id, case, 'short', 100, 10, CANDIDATE_SLEEP)
    else:
        jobs['background'] = standalone_job(run_id, case, 'background', 30, 300, HELPER_SLEEP)
        jobs['hold'] = standalone_job(run_id, case, 'hold', 70, 300, HELPER_SLEEP)
        jobs['large'] = standalone_job(run_id, case, 'large', 40, 10, CANDIDATE_SLEEP, pods=2, min_available=1)
        jobs['small'] = standalone_job(run_id, case, 'small', 60, 100, CANDIDATE_SLEEP)
    return jobs


def admission_usage(objects):
    """Sum Pod admission requests, including pending members (not allocations)."""
    usage = {key: 0 for key in PEAK_QUOTA}
    for obj in objects:
        if obj['kind'] != 'Pod':
            continue
        usage['pods'] += 1
        for container in obj['spec']['containers']:
            for resource, quantity in container['resources']['requests'].items():
                key = 'requests.' + resource
                if key in usage:
                    usage[key] += int(quantity)
    return usage


def protocol(run_id):
    cases = []
    for case in CASES:
        is_order = case.startswith('order-')
        jobs = scenario_jobs(run_id, case)
        mode = case[-1].upper()
        cases.append({
            'id': case, 'mode': mode, 'kind': 'order' if is_order else 'complete', 'jobs': jobs,
            'execution_enabled': is_order,
            'requires_compatibility_proof': None if is_order else 'full-job-node-lock',
            'blocked_reason': None if is_order else (
                'Paired complete-C is blocked by frozen NodeLock incompatibility; native partial case is prepared but not executed.'
                if case == 'complete-a' else
                'Frozen NodeLock=true multi-Pod speculative planning requires native compatibility proof; never disable NodeLock to force a pass.'),
            'helper_roles': ['blocker-0', 'blocker-1', 'blocker-2'] + ([] if is_order else ['background']) + ['hold'],
            'gate_role': 'hold', 'background_role': None if is_order else 'background',
            'candidate_roles': ['long', 'short'] if is_order else ['large', 'small'],
            'gap_seconds': 2, 'min_wait_seconds': 15, 'scenario_deadline_seconds': 240,
            'expected_initial_bind': ('long' if mode == 'A' else 'short') if is_order else ('large' if mode == 'A' else 'small'),
            'expected_complete_start_after_release': [p['metadata']['name'] for p in jobs['large'] if p['kind'] == 'Pod'] if case == 'complete-c' else None,
            'admission_peak': admission_usage([obj for docs in jobs.values() for obj in docs]),
            'readiness_rule': 'Wait for complete membership observed by the same scheduler process; in B/C require real HRRN snapshots. No ready_at injection or scheduler restart while waiting.',
            'release_rule': 'UID-precondition deletion of hold after recorded real wait; retain three blockers until candidates complete. Complete-mode eventual-start observation additionally requires background release.',
        })
    result = {
        'protocol_version': 1, 'run_id': _token(run_id, 'run_id'), 'experiment': EXPERIMENT,
        'system_namespace': SYSTEM_NS, 'test_namespace': TEST_NS, 'scheduler_name': SCHEDULER_NAME,
        'queue_name': QUEUE_NAME, 'node_name': NODE_NAME,
        'live_scope': ['order-a', 'order-b'],
        'configmaps': {mode: configmap_name(run_id, mode) for mode in configs()},
        'setup_objects': configmaps(run_id) + [scheduler_deployment(run_id)],
        'peak_quota': dict(PEAK_QUOTA), 'quota_semantics': 'Maximum Pod requests including pending members; never overlap scenarios or metadata propagation.',
        'cases': cases,
        'propagation': {'mode': 'B', 'execution_enabled': False,
                        'blocked_reason': 'Prepared metadata propagation case; not executed in this first live tranche.',
                        'object': propagation_job(run_id),
                        'expected_podgroup_annotations': {ESTIMATE: '37', TOTAL_PODS: '1'},
                        'association_rule': 'Find PodGroup by VolcanoJob UID owner reference, not a guessed generated name.'},
        'execution_constraints': [
            'Never apply the whole rendered directory; follow the bounded executor sequence.',
            'Never run independent vGPU schedulers concurrently for these devices.',
            'Run only after fresh foreign-GPU/DRA and private-reservation reconciliation; abort new tests on external demand.',
            'Native A has no HRRN tracker; record common external membership observations, not a fabricated internal ready_at.',
            'Capture actual comparator ties, selected task order, binding observation, start, completion, and release separately.',
            'Native per-Pod binding is not a cross-Pod atomic transaction.',
            'Only UID-checked experiment objects may be deleted; preserve original production and frozen RF state.',
            'Sleep tests demonstrate functionality, not training/inference performance or population P95 improvements.',
        ],
    }
    for case in cases:
        if any(case['admission_peak'][key] > int(value) for key, value in PEAK_QUOTA.items()):
            raise ValueError('Scenario exceeds declared quota')
    return result


def validate_manifest(obj):
    kind, meta = obj.get('kind'), obj.get('metadata', {})
    labels = meta.get('labels', {})
    if labels.get('gpu-lab/experiment') != EXPERIMENT or not labels.get(RUN_LABEL):
        raise ValueError('Missing experiment/run identity')
    _token(labels[RUN_LABEL], 'run_id')
    expected_ns = SYSTEM_NS if kind in ('Deployment', 'ConfigMap') else TEST_NS
    if meta.get('namespace') != expected_ns:
        raise ValueError('Manifest escapes experiment namespace')
    specs = []
    if kind == 'Pod':
        specs = [obj['spec']]
        if not meta.get('annotations', {}).get('scheduling.k8s.io/group-name'):
            raise ValueError('Standalone Pod must declare its group')
        if any(key.startswith('volcano.sh/') for key in meta.get('annotations', {})):
            raise ValueError('No preassigned device UUID, node, bind phase or quota annotations')
    elif kind == 'Job' and obj.get('apiVersion') == 'batch.volcano.sh/v1alpha1':
        if obj['spec'].get('schedulerName') != SCHEDULER_NAME or obj['spec'].get('queue') != QUEUE_NAME:
            raise ValueError('VolcanoJob uses the wrong scheduler or queue')
        specs = [task['template']['spec'] for task in obj['spec']['tasks']]
    elif kind == 'Deployment':
        if meta.get('name') != SCHEDULER_NAME or obj['spec'].get('replicas') != 0:
            raise ValueError('Experiment scheduler must be independent and default to zero replicas')
        spec = obj['spec']['template']['spec']
        if spec.get('serviceAccountName') != SERVICE_ACCOUNT:
            raise ValueError('Unexpected scheduler service account')
        containers = spec.get('containers', [])
        if {c['name']: c['image'] for c in containers} != {'scheduler': SCHEDULER_IMAGE, 'rf-model': RF_IMAGE}:
            raise ValueError('Scheduler/RF image differs from frozen digests')
        specs = [spec]
    elif kind == 'PodGroup':
        if obj['spec'].get('queue') != QUEUE_NAME or 'minResources' in obj['spec']:
            raise ValueError('Standalone PG must use the fixed queue without an enqueue capacity gate')
        n = int(meta.get('annotations', {}).get(TOTAL_PODS, '0'))
        if not 1 <= obj['spec'].get('minMember', 0) <= n:
            raise ValueError('Incomplete or invalid PG membership declaration')
        if int(meta.get('annotations', {}).get(ESTIMATE, '0')) <= 0:
            raise ValueError('Missing service estimate')
    elif kind != 'ConfigMap':
        raise ValueError('Unsupported experiment object kind')
    for spec in specs:
        if spec.get('nodeSelector') != {'kubernetes.io/hostname': NODE_NAME} or spec.get('nodeName'):
            raise ValueError('Use node selection, never prebind a Pod')
        if any(spec.get(key) for key in ('hostNetwork', 'hostPID', 'hostIPC', 'initContainers', 'ephemeralContainers', 'schedulingGates')):
            raise ValueError('Unsupported host, extra-container or artificial readiness mechanism')
        if any('hostPath' in volume for volume in spec.get('volumes', [])):
            raise ValueError('Experiment manifest must not mount host files')
        if spec.get('automountServiceAccountToken') is not False:
            raise ValueError('Unexpected automatic service account mount')
        for container in spec['containers']:
            if container.get('imagePullPolicy') != 'Never' or '@sha256:' not in container.get('image', ''):
                raise ValueError('Only prevalidated cached immutable images may run')
            if container.get('securityContext', {}).get('allowPrivilegeEscalation') is not False:
                raise ValueError('Privilege escalation must be disabled')
            if container.get('securityContext', {}).get('privileged') or container.get('securityContext', {}).get('capabilities', {}).get('add'):
                raise ValueError('Privileged execution and added capabilities are prohibited')
            if any(e.get('name', '').startswith(('NVIDIA_', 'CUDA_', 'GPU_')) for e in container.get('env', [])):
                raise ValueError('Do not override Device Plugin GPU visibility or quotas')
        if kind != 'Deployment':
            if spec.get('schedulerName') != SCHEDULER_NAME or spec.get('restartPolicy') != 'Never':
                raise ValueError('Unexpected workload scheduler or restart policy')
            if len(spec['containers']) != 1:
                raise ValueError('Exactly one sleep container per member is supported')
            container = spec['containers'][0]
            command = container.get('command', [])
            if len(command) != 2 or command[0] != '/bin/sleep' or not command[1].isdigit():
                raise ValueError('Only direct finite sleep execution is permitted')
            seconds = int(command[1])
            if not 1 <= seconds <= HELPER_SLEEP or not seconds < spec.get('activeDeadlineSeconds', 0) <= HELPER_SLEEP + 180:
                raise ValueError('Workload lifetime must be bounded')
            resources = container['resources']
            if resources['requests'].get('cpu') != '100m' or resources['requests'].get('memory') != '128Mi':
                raise ValueError('Candidate CPU and memory requests must remain comparable')
            for key, value in resources['requests'].items():
                if key.startswith('volcano.sh/vgpu-') and resources['limits'].get(key) != value:
                    raise ValueError('Extended-resource request/limit mismatch')
    return True


def render(run_id, output):
    plan = protocol(run_id)
    output.mkdir(parents=True, exist_ok=False)
    write = lambda path, value: path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    write(output / 'protocol.json', plan)
    write(output / 'setup.json', {'apiVersion': 'v1', 'kind': 'List', 'items': plan['setup_objects']})
    (output / 'configs').mkdir()
    for mode, config in configs().items():
        write(output / 'configs' / (mode + '.json'), config)
    for case in plan['cases']:
        case_dir = output / 'cases' / case['id']
        case_dir.mkdir(parents=True)
        for role, objects in case['jobs'].items():
            write(case_dir / (role + '.json'), {'apiVersion': 'v1', 'kind': 'List', 'items': objects})
    write(output / 'metadata-propagation.json', plan['propagation']['object'])
    (output / 'README.txt').write_text(
        'Local prepared manifests only. No cluster execution is implied.\n'
        'Do not apply this directory: jobs must be submitted in the recorded sequence.\n'
        'Scheduler defaults to zero replicas. Original scheduler must stop before this scheduler starts.\n'
        'A/B/C retain the same RF placement, native ledger, priority/gang/DRF/proportion and NodeLock=true.\n'
        'First live scope is order-a/order-b only; complete-a, complete-c and metadata propagation are prepared but disabled.\n'
        'Complete planning comparison is blocked until frozen NodeLock/full-plan compatibility is demonstrated.\n'
        'Never disable NodeLock to manufacture a pass; keep this failure boundary in the report.\n'
        'Temporary quota must cover pending requests: 8 Pods, 8 vGPU, 600 cores, 32768 MiB.\n'
        'Helpers sleep300, candidates sleep30, metadata-only test sleep5. No CUDA runs.\n'
        'All readiness and waiting evidence must come from actual observation in one scheduler process.\n', encoding='utf-8')
    return plan


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = render(args.run_id, args.output)
    print(json.dumps({'run_id': args.run_id, 'output': str(args.output), 'cases': [c['id'] for c in result['cases']],
                      'blocked_cases': [c['id'] for c in result['cases'] if not c['execution_enabled']],
                      'peak_quota': result['peak_quota'], 'cluster_executed': False}))
