"""Offline r3 upgrade checks; require a proven restored and empty r2 attempt."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path, PurePosixPath
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('image_repair_builder_tests', ROOT / 'scripts/build_hrrn_stage5_image_repair.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)
BASE, AUTHORIZATION = builder.previous.frozen_base()
CURRENT = builder.frozen_current()
boot = {'__name__': 'image_repair_unit_test'}
exec(compile(builder.bootstrap(BASE, AUTHORIZATION, CURRENT), 'image-repair-test-bootstrap', 'exec'), boot)
CHECKS = {'original', 'rf', 'dqn', 'quota', 'original_config', 'rf-config', 'geometry', 'dqn_available', 'model_sha'}


class ImageRepairSafeguards(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='hrrn-image-repair-test-')
        self.addCleanup(self.temp.cleanup)
        self.current = Path(self.temp.name) / 'r2'
        (self.current / 'evidence').mkdir(parents=True)
        self.state = dict(phase='restored', restored=True, created=[], planned=[], cases=[],
                          old_scale_intent=False, experiment_started=False)
        self.confirmation = dict(checks={key: True for key in CHECKS},
                                 original_process_sha=BASE['frozen_shas']['production_scheduler'],
                                 cluster_audit_retained=True, foreign_workloads_deleted=False)
        self.write_records()

    def write_records(self):
        (self.current / 'evidence/state.json').write_text(json.dumps(self.state))
        (self.current / 'evidence/restore-confirmation.json').write_text(json.dumps(self.confirmation))

    def test_recovered_r2_with_empty_ledger_is_accepted(self):
        boot['validate_previous_recovered'](self.current)

    def test_every_pending_or_started_state_blocks_upgrade(self):
        original = copy.deepcopy(self.state)
        for change in ({'phase': 'new'}, {'phase': 'executing'}, {'restored': False},
                       {'created': [{'uid': 'x'}]}, {'planned': [{'uid': 'x'}]}, {'cases': [{}]},
                       {'old_scale_intent': True}, {'experiment_started': True}):
            with self.subTest(change=change):
                self.state = {**original, **change}
                self.write_records()
                with self.assertRaisesRegex(RuntimeError, 'empty object/case ledger'):
                    boot['validate_previous_recovered'](self.current)

    def test_restore_checks_must_be_complete_and_exact_booleans(self):
        original = copy.deepcopy(self.confirmation)
        for key in CHECKS:
            for bad in (False, 1, 'true', None):
                with self.subTest(key=key, bad=bad):
                    self.confirmation = copy.deepcopy(original)
                    self.confirmation['checks'][key] = bad
                    self.write_records()
                    with self.assertRaises(RuntimeError):
                        boot['validate_previous_recovered'](self.current)
        self.confirmation = copy.deepcopy(original)
        del self.confirmation['checks']['model_sha']
        self.write_records()
        with self.assertRaises(RuntimeError):
            boot['validate_previous_recovered'](self.current)

    def test_restore_binary_audit_and_foreign_workload_evidence_required(self):
        original = copy.deepcopy(self.confirmation)
        for change in ({'original_process_sha': '0' * 64}, {'cluster_audit_retained': False},
                       {'foreign_workloads_deleted': True}):
            with self.subTest(change=change):
                self.confirmation = {**original, **change}
                self.write_records()
                with self.assertRaises(RuntimeError):
                    boot['validate_previous_recovered'](self.current)

    def test_payload_copy_uses_v1_and_preserves_both_previous_directories(self):
        base = Path(self.temp.name) / 'v1'
        destination = Path(self.temp.name) / 'r3'
        base.mkdir()
        baseline = copy.deepcopy(BASE)
        for name in baseline['files']:
            data = ('verified v1 fixture ' + name).encode()
            target = base / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            baseline['files'][name] = hashlib.sha256(data).hexdigest()
        (base / 'bundle-manifest.json').write_text(json.dumps(baseline))
        runner = b'new image-safe runner fixture'
        updated = copy.deepcopy(baseline)
        updated['install_root'] = builder.NEW_ROOT
        updated['files']['hrrn_stage5_live.py'] = hashlib.sha256(runner).hexdigest()
        def snapshot(directory):
            return {p.relative_to(directory).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
                    for p in directory.rglob('*') if p.is_file()}
        original_base, original_current = snapshot(base), snapshot(self.current)
        boot['validate_previous_recovered'](self.current)
        boot['copy_repaired_attempt'](base, destination, baseline, updated, runner)
        self.assertEqual(snapshot(base), original_base)
        self.assertEqual(snapshot(self.current), original_current)
        boot['verify_directory'](destination, updated)
        self.assertFalse((destination / 'evidence').exists())
        self.assertEqual((destination / 'hrrn_stage5_live.py').read_bytes(), runner)
        with self.assertRaises(FileExistsError):
            boot['copy_repaired_attempt'](base, destination, baseline, updated, runner)

    def test_r2_manifest_is_frozen_and_does_not_replace_v1_as_copy_source(self):
        self.assertEqual(boot['OLD_ROOT'], builder.previous.OLD_ROOT)
        self.assertEqual(boot['CURRENT_ROOT'], builder.CURRENT_ROOT)
        self.assertEqual(boot['NEW_ROOT'], builder.NEW_ROOT)
        self.assertEqual(boot['BASE_MANIFEST'], BASE)
        self.assertEqual(boot['CURRENT_MANIFEST'], CURRENT)
        self.assertEqual(CURRENT['files']['hrrn_stage5_live.py'], builder.CURRENT_RUNNER_SHA)
        self.assertEqual(BASE['execution_public_key'], CURRENT['execution_public_key'])
        for name in BASE['files']:
            if name != 'hrrn_stage5_live.py':
                self.assertEqual(BASE['files'][name], CURRENT['files'][name])

    def test_only_runner_and_fixed_r3_root_may_change(self):
        runner = b'fixture'
        manifest = copy.deepcopy(BASE)
        manifest['install_root'] = builder.NEW_ROOT
        manifest['files']['hrrn_stage5_live.py'] = hashlib.sha256(runner).hexdigest()
        boot['validate_change'](BASE, manifest, runner)
        manifest['files']['hrrn_stage5_gate.py'] = '0' * 64
        with self.assertRaises(ValueError):
            boot['validate_change'](BASE, manifest, runner)

    def test_key_redirect_preserves_other_access_and_fixed_restrictions(self):
        key = BASE['execution_public_key']
        old = boot['authorization_text']('# retain\nssh-ed25519 OTHER other\n', key,
                                         PurePosixPath(builder.CURRENT_ROOT), '20261027120000Z')
        updated = boot['authorization_text'](old, key, PurePosixPath(builder.NEW_ROOT), '20261027160000Z')
        self.assertIn('# retain\nssh-ed25519 OTHER other\n', updated)
        self.assertEqual(updated.count(key.split()[1]), 1)
        self.assertIn('restrict,expiry-time="20261027160000Z"', updated)
        self.assertIn('command="/usr/bin/python3 -I ' + builder.NEW_ROOT, updated)
        self.assertNotIn(builder.CURRENT_ROOT + '/hrrn_stage5_gate.py', updated)


if __name__ == '__main__':
    unittest.main(verbosity=2)
