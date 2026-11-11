"""Read-only prepare lifecycle tests and a real Linux packaged-import smoke test.

No server connection, Kubernetes subprocess, image import, or final package build.
Run with pytest on Windows or the standard-library unittest runner in Linux.
"""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))
from gpu_lab import staging

imports = {'staging': staging}
try:
    import fcntl
except ImportError:
    imports['fcntl'] = types.SimpleNamespace(LOCK_EX=2, LOCK_NB=4, LOCK_UN=8, flock=Mock())
with patch.dict(sys.modules, imports):
    spec = importlib.util.spec_from_file_location('preflight_test_live', ROOT / 'scripts/hrrn_stage5_live.py')
    live = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(live)


class PreflightLifecycle(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='hrrn-stage5-preflight-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        protocol = {'run_id': 'preflight-fixture', 'cases': [], 'setup_objects': [
            {'kind': 'Deployment', 'spec': {'template': {'spec': {'containers': [
                {'name': 'rf-service', 'image': 'example/frozen@sha256:' + '1' * 64}]}}}}]}
        (self.root / 'protocol.json').write_text(json.dumps(protocol))
        self.run = live.Run(self.root)
        self.generation = 1
        self.failure = None
        self.commands = []
        self.run.snapshot = self.snapshot
        self.run.get = self.get
        self.run.cmd = self.command
        self.run.process_sha = Mock(return_value=live.ORIGINAL_SHA)
        self.addCleanup(patch.stopall)
        patch.object(live.subprocess, 'run', side_effect=AssertionError('No real commands in preflight tests')).start()
        patch.object(live.subprocess, 'Popen', side_effect=AssertionError('No real processes in preflight tests')).start()

    def snapshot(self, name):
        snapshot = {'node_lock': None, 'devices': [{'allocated_vgpu_count': 0}] * 4}
        if self.failure == 'logical-lock':
            snapshot['node_lock'] = 'existing-lock'
        live.save(self.run.out / 'snapshots' / (name + '.json'), snapshot)
        return snapshot

    def get(self, kind, name=None, namespace=None, optional=False):
        if optional:
            return None
        if kind == 'node':
            return {'spec': {}, 'status': {'conditions': [{'type': 'Ready', 'status': 'True'}]}}
        if kind in ('resourceclaims.resource.k8s.io', 'deployments,statefulsets,daemonsets,replicasets,jobs.batch,cronjobs.batch'):
            return {'items': []}
        obj = {'metadata': {'name': name, 'uid': name + '-uid', 'resourceVersion': str(self.generation)}}
        if kind == 'deployment':
            replicas = 0 if name == 'volcano-rf-test' else 1
            obj.update(spec={'replicas': replicas}, status={'availableReplicas': replicas})
        elif kind == 'configmap':
            obj['data'] = {'generation': str(self.generation)}
        elif kind == 'resourcequota':
            obj['spec'] = {'hard': {'pods': '8'}}
        else:
            self.fail('Unexpected read: ' + str((kind, name)))
        return obj

    def command(self, args, **kwargs):
        self.commands.append(list(args))
        raw_checkpoint = json.dumps({'Data': {'PodDeviceEntries': [], 'RegisteredDevices': {}}})
        if args[:4] == ['sudo', '-n', 'stat', '-c']:
            text = str(len(raw_checkpoint.encode())) + ' 1790000000 2026-09-21 00:00:00.000000000 +0000 123'
        elif args[:3] == ['sudo', '-n', 'cat']:
            text = raw_checkpoint
        elif args[:3] == ['sudo', '-n', 'journalctl']:
            text = ''
        elif args[:4] == ['sudo', '-n', 'crictl', 'ps']:
            text = '{"containers": []}'
        elif args[:4] == ['sudo', '-n', 'crictl', 'pods']:
            text = '{"items": []}'
        elif args[:len(live.K)+1] == live.K + ['get']:
            text = '{"items": []}'
        elif args == ['nvidia-smi']:
            text = 'GPU process present' if self.failure == 'gpu-process' else 'No running processes found'
        elif args[0] == 'sha256sum':
            text = live.MODEL_SHA + '  frozen.joblib'
        elif args[:len(live.K)] == live.K and args[len(live.K):len(live.K)+2] == ['auth', 'can-i']:
            text = 'no' if self.failure == 'permission' else 'yes'
        elif args[:4] == ['sudo', '-n', 'crictl', 'inspecti']:
            if self.failure == 'image':
                raise RuntimeError('Image is absent from local cache')
            text = '{}'
        else:
            self.fail('Unapproved or mutating preflight command: ' + repr(args))
        return types.SimpleNamespace(stdout=text, stderr='', returncode=0)

    def test_failed_preflight_never_commits_recovery_originals(self):
        for failure in ('logical-lock', 'gpu-process', 'permission', 'image'):
            with self.subTest(failure=failure):
                self.failure = failure
                with self.assertRaises(RuntimeError):
                    self.run.prepare()
                self.assertFalse((self.run.out / 'backups').exists())
                self.assertEqual(self.run.state['phase'], 'new')
        self.assertEqual(self.run.state['preflight_attempts'], 4)
        self.assertTrue((self.run.out / 'preflight/3-observed-originals.json').exists())
        self.assertTrue((self.run.out / 'preflight/4-observed-originals.json').exists())

    def test_retry_commits_only_fresh_successful_originals_and_keeps_history(self):
        self.failure = 'permission'
        with self.assertRaises(RuntimeError):
            self.run.prepare()
        first = (self.run.out / 'preflight/1-observed-originals.json').read_bytes()
        self.failure = None
        self.generation = 2
        self.run.prepare()
        self.assertEqual(self.run.state['phase'], 'prepared')
        self.assertEqual(self.run.state['preflight_attempts'], 2)
        self.assertEqual(len(list((self.run.out / 'backups').glob('*.json'))), 7)
        self.assertEqual(self.run.prior('original')['metadata']['resourceVersion'], '2')
        self.assertEqual((self.run.out / 'preflight/1-observed-originals.json').read_bytes(), first)
        self.assertTrue((self.run.out / 'snapshots/initial-1.json').exists())
        self.assertTrue((self.run.out / 'snapshots/initial-2.json').exists())
        with self.assertRaisesRegex(RuntimeError, 'already prepared'):
            self.run.prepare()

    def test_scheduler_with_binding_only_permission_can_prepare(self):
        command = self.command
        checked = []
        def least_privilege(args, **kwargs):
            impersonated = '--as=system:serviceaccount:gpu-rf-system:volcano-rf-test' in args
            if impersonated and args[len(live.K):len(live.K)+3] == ['auth', 'can-i', 'create']:
                resource = args[len(live.K)+3]
                resource_type, _, resource_name = resource.partition('/')
                subresource = next((a.split('=', 1)[1] for a in args if a.startswith('--subresource=')), '')
                if resource_type == 'pods':
                    checked.append((resource_type, resource_name, subresource))
                    if resource_name or subresource != 'binding':
                        raise RuntimeError('Least privilege permits binding existing Pods, not creating a Pod')
            return command(args, **kwargs)
        self.run.cmd = least_privilege
        self.run.prepare()
        self.assertEqual(self.run.state['phase'], 'prepared')
        self.assertEqual(checked, [('pods', '', 'binding')])

    def test_unsupported_checkpoint_evidence_read_stops_prepare_without_mutation(self):
        reads = [
            ['sudo', '-n', 'stat'], ['sudo', '-n', 'cat'],
            live.K + ['get', 'pods'],
            live.K + ['get', '--raw=/api/v1/nodes/t3dgq/proxy/pods'],
            ['sudo', '-n', 'crictl', 'ps', '-a'], ['sudo', '-n', 'crictl', 'ps', '-o'],
            ['sudo', '-n', 'crictl', 'pods'], ['sudo', '-n', 'journalctl'],
        ]
        for prefix in reads:
            with self.subTest(read=prefix):
                def unsupported(args, **kwargs):
                    if args[:len(prefix)] == prefix:
                        raise RuntimeError('Required read is unsupported')
                    return self.command(args, **kwargs)
                self.run.cmd = unsupported
                with self.assertRaisesRegex(RuntimeError, 'Required read is unsupported'):
                    self.run.prepare()
                self.assertEqual(self.run.state['phase'], 'new')
                self.assertFalse((self.run.out / 'backups').exists())
                self.assertFalse(self.run.state.get('created'))
                self.assertFalse(self.run.state.get('experiment_started'))
        self.assertEqual(self.run.state['preflight_attempts'], len(reads))

    def test_controlled_wait_has_a_deadline_without_real_sleep(self):
        clock = [0.0]
        def advance(seconds):
            clock[0] += seconds
        self.run.guard = Mock()
        condition = Mock(return_value=False)
        with patch.object(live.time, 'monotonic', lambda: clock[0]), patch.object(live.time, 'sleep', advance):
            with self.assertRaisesRegex(RuntimeError, 'Timed out'):
                self.run.wait(condition, seconds=2)
        self.assertLessEqual(condition.call_count, 4)
        self.assertLessEqual(clock[0], 3)

    def test_global_window_limit_fails_before_any_cluster_read(self):
        self.run.state['started_epoch'] = 1.0
        self.run.get = Mock(side_effect=AssertionError('Expired run performed a cluster read'))
        with patch.object(live.time, 'time', return_value=live.WINDOW_SECONDS + 2.0):
            with self.assertRaisesRegex(RuntimeError, 'window exceeded'):
                self.run.guard()
        self.run.get.assert_not_called()


@unittest.skipUnless(sys.platform.startswith('linux'), 'Real Linux fcntl/proc runtime smoke test')
class LinuxPackagedImports(unittest.TestCase):
    def test_isolated_gate_spawns_plain_python_with_all_runtime_modules(self):
        import hrrn_stage5_gate as gate
        source = {
            'hrrn_stage5_gate.py': ROOT / 'scripts/hrrn_stage5_gate.py',
            'hrrn_stage5_live.py': ROOT / 'scripts/hrrn_stage5_live.py',
            'hrrn_stage5_evidence.py': ROOT / 'scripts/hrrn_stage5_evidence.py',
            'helpers/staging.py': ROOT / 'gpu_lab/staging.py',
            'helpers/round7_collect.py': ROOT / 'scripts/round7_collect.py',
            'helpers/round7_window_preflight.py': ROOT / 'scripts/round7_window_preflight.py',
            'gpu_lab/__init__.py': ROOT / 'gpu_lab/__init__.py',
            'gpu_lab/staging.py': ROOT / 'gpu_lab/staging.py',
        }
        self.assertEqual(set(source), {p for p in gate.FILES if p.endswith('.py')})
        with tempfile.TemporaryDirectory(prefix='hrrn-stage5-import-smoke-') as directory:
            root = Path(directory)
            for name, path in source.items():
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(path.read_bytes())
            code = '''import importlib.util, pathlib, sys
root = pathlib.Path(sys.argv[1])
assert sys.flags.isolated == 1
spec = importlib.util.spec_from_file_location('installed_gate', root / 'hrrn_stage5_gate.py')
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)
raise SystemExit(gate.Gate(root).foreground('--help'))
'''
            result = subprocess.run([sys.executable, '-I', '-c', code, str(root)], capture_output=True,
                                    text=True, timeout=20, cwd=root, env=gate.ENV)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('--prepare', result.stdout)
            self.assertIn('--execute', result.stdout)
            # Exercise the lazy evidence import under the same normal-script path
            # setup that live uses after the gate launches its child interpreter.
            probe = root / 'import_probe.py'
            probe.write_text('import hrrn_stage5_live\nimport hrrn_stage5_evidence\n'
                             'import gpu_lab.staging\nimport fcntl\nprint("LINUX_PAYLOAD_IMPORTS_OK")\n')
            result = subprocess.run([sys.executable, str(probe)], capture_output=True, text=True,
                                    timeout=20, cwd=root, env=gate.ENV)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), 'LINUX_PAYLOAD_IMPORTS_OK')
            self.assertEqual(set(p.name for p in (root / 'evidence').iterdir()), set())


if __name__ == '__main__':
    unittest.main(verbosity=2)
