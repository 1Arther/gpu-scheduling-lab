"""Offline runner-only repair safeguards. No installation or remote operations."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('stage5_repair_builder', ROOT / 'scripts/build_hrrn_stage5_repair.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)
FROZEN, AUTHORIZATION = builder.frozen_base()
boot = {'__name__': 'repair_unit_test'}
exec(compile(builder.bootstrap(FROZEN, AUTHORIZATION), 'repair-test-bootstrap', 'exec'), boot)


class RepairSafeguards(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='hrrn-repair-test-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name) / 'old'
        self.destination = Path(self.temp.name) / 'new'
        self.base.mkdir()
        self.manifest = copy.deepcopy(FROZEN)
        for name in self.manifest['files']:
            data = ('frozen fixture ' + name).encode()
            path = self.base / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            self.manifest['files'][name] = hashlib.sha256(data).hexdigest()
        (self.base / 'bundle-manifest.json').write_text(json.dumps(self.manifest))
        (self.base / 'evidence').mkdir()
        self.state = {'phase': 'new', 'created': [], 'planned': [], 'old_scale_intent': False,
                      'experiment_started': False, 'preflight_attempts': 1}
        (self.base / 'evidence/state.json').write_text(json.dumps(self.state))
        (self.base / 'evidence/events.jsonl').write_text('{"error":"read-only preflight"}\n')
        self.runner = b'# corrected runner fixture\n'
        self.updated = copy.deepcopy(self.manifest)
        self.updated['install_root'] = builder.NEW_ROOT
        self.updated['files']['hrrn_stage5_live.py'] = hashlib.sha256(self.runner).hexdigest()

    def snapshot(self):
        return {p.relative_to(self.base).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in self.base.rglob('*') if p.is_file()}

    def test_old_attempt_and_evidence_unchanged_and_only_runner_replaced(self):
        original = self.snapshot()
        boot['copy_repaired_attempt'](self.base, self.destination, self.manifest, self.updated, self.runner)
        self.assertEqual(self.snapshot(), original)
        self.assertFalse((self.destination / 'evidence').exists())
        boot['verify_directory'](self.destination, self.updated)
        for name in self.manifest['files']:
            expected = self.runner if name == 'hrrn_stage5_live.py' else (self.base / name).read_bytes()
            self.assertEqual((self.destination / name).read_bytes(), expected)

    def test_corrupt_base_payload_rejected_before_new_directory_creation(self):
        for name in ('scheduler.oci.tar', 'hrrn_stage5_gate.py', 'hrrn_stage5_live.py'):
            with self.subTest(name=name):
                path = self.base / name
                original = path.read_bytes()
                path.write_bytes(b'corrupt')
                with self.assertRaisesRegex(ValueError, 'hash mismatch'):
                    boot['copy_repaired_attempt'](self.base, self.destination, self.manifest, self.updated, self.runner)
                self.assertFalse(self.destination.exists())
                path.write_bytes(original)

    def test_installed_manifest_corruption_is_not_adopted(self):
        (self.base / 'bundle-manifest.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'manifest differs'):
            boot['copy_repaired_attempt'](self.base, self.destination, self.manifest, self.updated, self.runner)
        self.assertFalse(self.destination.exists())

    def test_existing_destination_refused_and_not_overwritten(self):
        self.destination.mkdir()
        marker = self.destination / 'preserve'
        marker.write_text('prior attempt')
        with self.assertRaises(FileExistsError):
            boot['copy_repaired_attempt'](self.base, self.destination, self.manifest, self.updated, self.runner)
        self.assertEqual(marker.read_text(), 'prior attempt')
        self.assertEqual(list(self.destination.iterdir()), [marker])

    def test_unrelated_file_key_scope_and_path_changes_rejected(self):
        variants = []
        changed = copy.deepcopy(self.updated); changed['files']['hrrn_stage5_gate.py'] = '0' * 64; variants.append(changed)
        changed = copy.deepcopy(self.updated); changed['files']['new.py'] = '0' * 64; variants.append(changed)
        changed = copy.deepcopy(self.updated); changed['execution_public_key'] = 'another key'; variants.append(changed)
        changed = copy.deepcopy(self.updated); changed['live_scope'] = ['complete-c']; variants.append(changed)
        changed = copy.deepcopy(self.updated); changed['install_root'] = '/tmp/arbitrary'; variants.append(changed)
        for changed in variants:
            with self.subTest(changed=changed):
                with self.assertRaises(ValueError):
                    boot['validate_change'](self.manifest, changed, self.runner)
        with self.assertRaises(ValueError):
            boot['validate_change'](self.manifest, self.updated, b'wrong runner')

    def test_executed_or_active_attempt_cannot_be_upgraded(self):
        boot['validate_not_started'](self.base)
        for change in ({'phase': 'prepared'}, {'created': [{'uid': 'one'}]}, {'planned': [{'name': 'one'}]},
                       {'old_scale_intent': True}, {'experiment_started': True}, {'started_epoch': 123}):
            with self.subTest(change=change):
                (self.base / 'evidence/state.json').write_text(json.dumps({**self.state, **change}))
                with self.assertRaises(RuntimeError):
                    boot['validate_not_started'](self.base)
        (self.base / 'evidence/state.json').write_text(json.dumps(self.state))
        (self.base / 'evidence/launch.json').write_text('{}')
        with self.assertRaisesRegex(RuntimeError, 'execution was attempted'):
            boot['validate_not_started'](self.base)

    def test_patch_archive_cannot_add_any_payload_member(self):
        patch = Path(self.temp.name) / 'bad.pyz'
        with zipfile.ZipFile(patch, 'w') as z:
            z.writestr('__main__.py', '')
            z.writestr('repair-manifest.json', '{}')
            z.writestr('payload/hrrn_stage5_live.py', '')
            z.writestr('payload/other.py', '')
        with self.assertRaisesRegex(ValueError, 'only replacement'):
            boot['read_repair'](patch)

    def test_authorization_redirects_same_key_without_expanding_command_surface(self):
        key = FROZEN['execution_public_key']
        old = boot['authorization_text']('# preserved comment\nssh-ed25519 OTHER other\n', key,
                                         Path(builder.OLD_ROOT), '20261027120000Z')
        updated = boot['authorization_text'](old, key, Path(builder.NEW_ROOT), '20261027160000Z')
        self.assertIn('# preserved comment\nssh-ed25519 OTHER other\n', updated)
        self.assertEqual(updated.count(key.split()[1]), 1)
        self.assertIn('restrict,expiry-time="20261027160000Z"', updated)
        self.assertIn('command="/usr/bin/python3 -I ', updated)
        self.assertIn('hrrn-stage5-20261027-live01-r2', updated)
        self.assertNotIn('hrrn-stage5-20261027-live01/hrrn_stage5_gate.py', updated)


if __name__ == '__main__':
    unittest.main(verbosity=2)
