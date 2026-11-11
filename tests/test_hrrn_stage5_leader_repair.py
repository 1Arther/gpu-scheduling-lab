"""Only the audited r3 A-pass/Lease-handoff failure can authorize the r4 copy."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('leader_repair_tests_builder', ROOT / 'scripts/build_hrrn_stage5_leader_repair.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)
BASE, AUTHORIZATION = builder.previous.previous.frozen_base()
CURRENT = builder.frozen_current()
with zipfile.ZipFile(builder.previous.previous.BASE_PACKAGE) as archive:
    PROTOCOL = json.loads(archive.read('payload/protocol.json'))
boot = {'__name__': 'leader_repair_tests'}
exec(compile(builder.bootstrap(BASE, AUTHORIZATION, CURRENT), 'leader-test-bootstrap', 'exec'), boot)
ERROR = "RuntimeError('Lease is not held by the observed experiment Pod')"


class LeaderRepairSafeguards(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='hrrn-leader-repair-test-')
        self.addCleanup(self.temp.cleanup)
        self.current = Path(self.temp.name)
        (self.current / 'evidence').mkdir()
        (self.current / 'protocol.json').write_text(json.dumps(PROTOCOL))
        native = next(c for c in PROTOCOL['cases'] if c['id'] == 'order-a')
        long_name = next(o['metadata']['name'] for o in native['jobs']['long'] if o['kind'] == 'Pod')
        docs = list(PROTOCOL['setup_objects']) + [o for group in native['jobs'].values() for o in group]
        docs.append({'kind': 'Lease', 'metadata': {'namespace': 'gpu-rf-system', 'name': 'volcano-hrrn-stage5'}})
        rows = [dict(kind=o['kind'], namespace=o['metadata']['namespace'], name=o['metadata']['name'], uid='uid-' + str(i))
                for i, o in enumerate(docs)]
        self.state = dict(run_id=PROTOCOL['run_id'], phase='restored', restored=True, experiment_success=False,
                          error=ERROR, old_scale_intent=True, experiment_started=True,
                          cases=[dict(case='order-a', mode='A', ordering_matched=True,
                                      first_pod=long_name, expected_first_pod=long_name)],
                          created=rows, planned=[{k: v for k, v in row.items() if k != 'uid'} for row in rows])
        self.events = [dict(event='deleted', **row) for row in rows] + [dict(event='experiment_error', error=ERROR)]
        self.confirmation = dict(checks={name: True for name in ('original', 'rf', 'dqn', 'quota', 'original_config',
                                                               'rf-config', 'geometry', 'dqn_available', 'model_sha')},
                                 original_process_sha=BASE['frozen_shas']['production_scheduler'],
                                 cluster_audit_retained=True, foreign_workloads_deleted=False)
        self.write()

    def write(self):
        (self.current / 'evidence/state.json').write_text(json.dumps(self.state))
        (self.current / 'evidence/restore-confirmation.json').write_text(json.dumps(self.confirmation))
        (self.current / 'evidence/events.jsonl').write_text(''.join(json.dumps(e) + '\n' for e in self.events))

    def validate(self):
        self.write()
        boot['validate_previous_recovered'](self.current)

    def test_exact_restored_a_pass_lease_failure_is_accepted(self):
        # Historical flags remain true after real A execution and do not imply a
        # still-running attempt; restored evidence and complete UID cleanup do.
        self.validate()

    def test_arbitrary_failure_success_or_nonrestored_state_is_rejected(self):
        original = copy.deepcopy(self.state)
        for change in ({'error': "RuntimeError('other failure')"}, {'experiment_success': True},
                       {'phase': 'recovery_needs_attention'}, {'restored': False}):
            with self.subTest(change=change):
                self.state = {**original, **change}
                with self.assertRaises(RuntimeError):
                    self.validate()

    def test_result_must_be_exactly_native_a_long_first(self):
        original = copy.deepcopy(self.state['cases'])
        for change in ({'case': 'order-b'}, {'mode': 'B'}, {'ordering_matched': False},
                       {'first_pod': 'short'}, {'expected_first_pod': 'short'}):
            with self.subTest(change=change):
                self.state['cases'] = [{**original[0], **change}]
                with self.assertRaises(RuntimeError):
                    self.validate()
        for cases in ([], original + original):
            self.state['cases'] = cases
            with self.assertRaises(RuntimeError):
                self.validate()

    def test_b_pod_or_group_forbidden_even_with_matching_deletion(self):
        original_state, original_events = copy.deepcopy(self.state), copy.deepcopy(self.events)
        b = next(c for c in PROTOCOL['cases'] if c['id'] == 'order-b')
        for obj in b['jobs']['long']:
            with self.subTest(kind=obj['kind']):
                self.state, self.events = copy.deepcopy(original_state), copy.deepcopy(original_events)
                row = dict(kind=obj['kind'], namespace=obj['metadata']['namespace'], name=obj['metadata']['name'], uid='b-uid')
                self.state['created'].append(row)
                self.state['planned'].append({k: v for k, v in row.items() if k != 'uid'})
                self.events.append(dict(event='deleted', **row))
                with self.assertRaisesRegex(RuntimeError, 'B/foreign'):
                    self.validate()

    def test_all_four_deletion_identity_fields_must_match(self):
        original = copy.deepcopy(self.events)
        for field in ('kind', 'namespace', 'name', 'uid'):
            with self.subTest(field=field):
                self.events = copy.deepcopy(original)
                self.events[0][field] = 'different'
                with self.assertRaisesRegex(RuntimeError, 'deleted event'):
                    self.validate()
        self.events = original[1:]
        with self.assertRaisesRegex(RuntimeError, 'deleted event'):
            self.validate()

    def test_foreign_deletion_and_duplicate_object_uids_are_rejected(self):
        original = copy.deepcopy(self.events)
        self.events.append(dict(event='deleted', kind='Pod', namespace='foreign', name='other', uid='other'))
        with self.assertRaises(RuntimeError):
            self.validate()
        self.events = original
        self.state['created'][1]['uid'] = self.state['created'][0]['uid']
        with self.assertRaises(RuntimeError):
            self.validate()

    def test_unfinished_or_unknown_plans_are_rejected(self):
        self.state['planned'].append(dict(kind='Pod', namespace='gpu-rf-tests', name='unknown-pending'))
        with self.assertRaisesRegex(RuntimeError, 'B/foreign'):
            self.validate()

    def test_missing_extra_or_different_failure_event_is_rejected(self):
        original = copy.deepcopy(self.events)
        for failure_events in ([], [dict(event='experiment_error', error='other')],
                               [dict(event='experiment_error', error=ERROR)] * 2):
            self.events = original[:-1] + failure_events
            with self.assertRaisesRegex(RuntimeError, 'single confirmed'):
                self.validate()

    def test_restoration_checks_must_be_complete_true_and_for_frozen_binary(self):
        original = copy.deepcopy(self.confirmation)
        variants = []
        changed = copy.deepcopy(original); changed['checks']['model_sha'] = False; variants.append(changed)
        changed = copy.deepcopy(original); changed['checks']['quota'] = 1; variants.append(changed)
        changed = copy.deepcopy(original); del changed['checks']['rf']; variants.append(changed)
        changed = copy.deepcopy(original); changed['original_process_sha'] = 'unknown'; variants.append(changed)
        for variant in variants:
            self.confirmation = variant
            with self.assertRaisesRegex(RuntimeError, 'restoration confirmation'):
                self.validate()

    def test_fixed_source_previous_manifest_target_and_key_unchanged(self):
        self.assertEqual(boot['OLD_ROOT'], '/home/zbs/hrrn-stage5-20261027-live01')
        self.assertEqual(boot['CURRENT_ROOT'], '/home/zbs/hrrn-stage5-20261027-live01-r3')
        self.assertEqual(boot['NEW_ROOT'], '/home/zbs/hrrn-stage5-20261027-live01-r4')
        self.assertEqual(boot['CURRENT_MANIFEST'], CURRENT)
        self.assertEqual(CURRENT['files']['hrrn_stage5_live.py'], builder.CURRENT_RUNNER_SHA)
        self.assertEqual(CURRENT['execution_public_key'], BASE['execution_public_key'])
        self.assertEqual(CURRENT['files']['hrrn_stage5_gate.py'], BASE['files']['hrrn_stage5_gate.py'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
