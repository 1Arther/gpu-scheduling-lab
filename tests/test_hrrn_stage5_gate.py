"""Portable tests for the exact-command execution boundary and zipapp bootstrap."""
import copy
import base64
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import stat
import tarfile
from types import SimpleNamespace
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = load('tested_stage5_gate', ROOT / 'scripts/hrrn_stage5_gate.py')
builder = load('tested_stage5_builder', ROOT / 'scripts/build_hrrn_stage5_package.py')
boot = {'__name__': 'bootstrap_test'}
exec(compile(builder.BOOTSTRAP, 'bootstrap', 'exec'), boot)
manifests = load('tested_gate_manifests', ROOT / 'scripts/hrrn_stage5_manifests.py')


@pytest.mark.parametrize('command', ['', 'bash', 'hrrn-stage5 start --again', 'hrrn-stage5 status; id',
                                      'hrrn-stage5 recover\nwhoami', 'scp -t /tmp/x',
                                      'hrrn-stage5 export /home/zbs/.ssh', 'hrrn-stage5 delete'])
def test_unknown_commands_and_extra_arguments_never_dispatch(command, monkeypatch, tmp_path):
    def forbidden(*args):
        raise AssertionError('Identity/runner dispatch reached for an unlisted command')
    monkeypatch.setattr(gate, 'require_identity', forbidden)
    with pytest.raises(ValueError, match='Denied'):
        gate.dispatch(command, tmp_path)


@pytest.mark.parametrize('name', ['../x', '/etc/passwd', 'C:/private', 'a\\b', 'a/../x', 'a//x', './x'])
def test_member_path_traversal_rejected_by_both_boundaries(name):
    with pytest.raises(ValueError):
        gate.relative_name(name)
    with pytest.raises(ValueError):
        boot['safe_member'](name)


@pytest.mark.parametrize('name,uid,home', [('root', 0, '/root'), ('zbs', 0, '/home/zbs'),
                                        ('other', 1001, '/home/other'), ('zbs', 1000, '/tmp')])
def test_bootstrap_requires_expected_nonroot_identity(name, uid, home):
    with pytest.raises(PermissionError):
        boot['require_installer_identity'](name, uid, home)


def test_gate_rejects_wrong_uid_even_for_correct_username():
    with pytest.raises(PermissionError):
        gate.identity_policy('zbs', 1000, 1001)
    gate.identity_policy('zbs', 1000, 1000)


def small_archive(tmp_path, *, bad_hash=False, extra=None, duplicate=False, symlink=False):
    content = b'reviewed payload'
    manifest = dict(install_root=builder.INSTALL_ROOT, live_scope=['order-a', 'order-b'],
                    files={'one.txt': hashlib.sha256(content).hexdigest()})
    path = tmp_path / 'bundle.pyz'
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('__main__.py', '# bootstrap fixture')
        z.writestr('install-manifest.json', json.dumps(manifest))
        z.writestr('payload/one.txt', b'tampered' if bad_hash else content)
        if extra:
            z.writestr(extra, 'extra')
        if duplicate:
            z.writestr('payload/one.txt', content)
        if symlink:
            entry = zipfile.ZipInfo('payload/link')
            entry.create_system = 3
            entry.external_attr = (stat.S_IFLNK | 0o777) << 16
            z.writestr(entry, '/etc/passwd')
    return path


def test_archive_integrity_checked_before_extraction(tmp_path):
    path = small_archive(tmp_path)
    manifest, payload = boot['validated_payload'](path)
    assert payload == {'one.txt': b'reviewed payload'}
    assert manifest['install_root'] == builder.INSTALL_ROOT
    assert list(tmp_path.iterdir()) == [path]  # No extraction occurs during validation.


@pytest.mark.parametrize('options', [dict(bad_hash=True), dict(extra='payload/../../escape'),
                                    dict(extra='unapproved.txt'), dict(duplicate=True), dict(symlink=True)])
def test_malformed_archive_refused_without_extraction(tmp_path, options):
    with pytest.warns(UserWarning) if options.get('duplicate') else __import__('contextlib').nullcontext():
        path = small_archive(tmp_path, **options)
    with pytest.raises(ValueError):
        boot['validated_payload'](path)
    assert list(tmp_path.iterdir()) == [path]


def test_private_key_input_refused(tmp_path):
    path = tmp_path / 'accidental-private-key'
    path.write_text('-----BEGIN OPENSSH PRIVATE KEY-----\nPRIVATE\n-----END OPENSSH PRIVATE KEY-----')
    with pytest.raises(ValueError):
        builder.public_key(path)


def test_authorized_keys_preserves_existing_access_and_scopes_new_key():
    blob = base64.b64encode(b'\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20' + b'x' * 32).decode()
    key = 'ssh-ed25519 ' + blob + ' gpu-lab-hrrn-stage5-live01'
    original = '# retain comment\nrestrict,command="old-readonly" ssh-ed25519 OTHER_BLOB existing\n'
    text = boot['authorization_text'](original, key, Path('/home/zbs/fixed'), '20261027080000Z')
    assert text.startswith(original)
    assert text.count(blob) == 1
    assert 'restrict,expiry-time="20261027080000Z"' in text
    assert 'command="/usr/bin/python3 -I ' in text
    assert text.endswith(key + '\n')
    assert boot['authorization_text'](text, key, Path('/home/zbs/fixed'), '20261027090000Z').count(blob) == 1
    with pytest.raises(ValueError):
        boot['authorization_text'](original, key + '\nssh-ed25519 injected', Path('/home/zbs/fixed'), 'x')


def test_unreviewed_complete_case_or_scope_rejected():
    p = manifests.protocol('test-001')
    gate.validate_protocol(p)
    changed = copy.deepcopy(p)
    changed['cases'][-1]['execution_enabled'] = True
    with pytest.raises(ValueError):
        gate.validate_protocol(changed)
    changed = copy.deepcopy(p)
    changed['propagation']['execution_enabled'] = True
    with pytest.raises(ValueError):
        gate.validate_protocol(changed)


def prepared_gate(tmp_path):
    g = gate.Gate(tmp_path)
    (g.out / 'state.json').write_text(json.dumps({'phase': 'prepared'}))
    return g


def test_exclusive_launch_intent_prevents_double_start(monkeypatch, tmp_path):
    g = prepared_gate(tmp_path)
    calls = []
    monkeypatch.setattr(gate, 'verify_bundle', lambda root: None)
    monkeypatch.setattr(gate, 'process_status', lambda root, record: dict(alive=True, pid=123, start_ticks='44'))
    def popen(*args, **kwargs):
        assert g.launch.exists(), 'Durable intent must precede Popen'
        assert kwargs['start_new_session'] is True
        calls.append(args)
        return SimpleNamespace(pid=123)
    monkeypatch.setattr(gate.subprocess, 'Popen', popen)
    assert g.start() == 0
    with pytest.raises(FileExistsError):
        g.start()
    assert len(calls) == 1
    assert json.loads(g.launch.read_text())['start_ticks'] == '44'


def test_failed_launch_cannot_be_retried_silently(monkeypatch, tmp_path):
    g = prepared_gate(tmp_path)
    monkeypatch.setattr(gate, 'verify_bundle', lambda root: None)
    monkeypatch.setattr(gate.subprocess, 'Popen', lambda *a, **k: (_ for _ in ()).throw(OSError('simulated spawn failure')))
    with pytest.raises(OSError):
        g.start()
    assert json.loads(g.launch.read_text())['status'] == 'launch_failed_or_uncertain'
    with pytest.raises(FileExistsError):
        g.start()


def test_prepare_is_required_before_start(monkeypatch, tmp_path):
    g = gate.Gate(tmp_path)
    monkeypatch.setattr(gate, 'verify_bundle', lambda root: None)
    with pytest.raises(RuntimeError, match='prepared'):
        g.start()
    assert not g.launch.exists()


def test_recovery_of_running_runner_requests_stop_without_signals(monkeypatch, tmp_path):
    g = prepared_gate(tmp_path)
    monkeypatch.setattr(g, 'runner', lambda: {'alive': True, 'pid': 123})
    monkeypatch.setattr(g, 'foreground', lambda flag: (_ for _ in ()).throw(AssertionError('Concurrent recover dispatched')))
    monkeypatch.setattr(gate.os, 'kill', lambda *a: (_ for _ in ()).throw(AssertionError('Gate must not signal processes')))
    assert g.recover() == 0
    assert (tmp_path / 'STOP').exists()


def test_export_contains_only_evidence_and_protocol(monkeypatch, tmp_path):
    g = prepared_gate(tmp_path)
    (tmp_path / 'protocol.json').write_text('{}')
    (g.out / 'events.jsonl').write_text('{"event":"test"}\n')
    (tmp_path / 'private-key').write_text('must not export')
    (tmp_path / 'scheduler.oci.tar').write_text('must not export')
    output = io.BytesIO()
    g.export(output)
    with tarfile.open(fileobj=io.BytesIO(output.getvalue()), mode='r:gz') as t:
        assert set(t.getnames()) == {'protocol.json', 'evidence/state.json', 'evidence/events.jsonl'}


def test_export_refuses_symlink_paths_without_following_them(monkeypatch, tmp_path):
    g = prepared_gate(tmp_path)
    (tmp_path / 'protocol.json').write_text('{}')
    (g.out / 'outside-link').write_text('fixture')
    original = Path.is_symlink
    monkeypatch.setattr(Path, 'is_symlink', lambda p: p.name == 'outside-link' or original(p))
    with pytest.raises(ValueError, match='Symlink'):
        g.export(io.BytesIO())


def test_recorded_pid_with_unrelated_command_is_not_trusted(tmp_path):
    root = tmp_path / 'root'; root.mkdir()
    proc = tmp_path / 'proc'; pid = proc / '123'; pid.mkdir(parents=True)
    (pid / 'cmdline').write_bytes(b'/usr/bin/python3\0someone-else.py\0--execute\0')
    (pid / 'stat').write_text('123 (python3) S ' + ' '.join(['0'] * 19))
    with pytest.raises(RuntimeError, match='no longer identifies'):
        gate.process_status(root, {'pid': 123}, proc)


def test_reused_pid_is_not_trusted_for_recovery(tmp_path):
    root = tmp_path / 'root'; root.mkdir()
    proc = tmp_path / 'proc'; pid = proc / '123'; pid.mkdir(parents=True)
    (pid / 'cmdline').write_bytes(str(root / 'hrrn_stage5_live.py').encode() + b'\0--execute\0')
    (pid / 'stat').write_text('123 (python3) S ' + ' '.join(['0'] * 18) + ' 42')
    with pytest.raises(RuntimeError, match='reused'):
        gate.process_status(root, {'pid': 123, 'start_ticks': '41'}, proc)
    assert gate.process_status(root, {'pid': 123, 'start_ticks': '42'}, proc)['alive'] is True


def test_process_owned_by_other_uid_is_not_trusted(tmp_path, monkeypatch):
    root = tmp_path / 'root'; root.mkdir()
    proc = tmp_path / 'proc'; pid = proc / '123'; pid.mkdir(parents=True)
    original = Path.stat
    monkeypatch.setattr(Path, 'stat', lambda p, *a, **k: SimpleNamespace(st_uid=original(root).st_uid + 1)
                        if p == pid else original(p, *a, **k))
    with pytest.raises(PermissionError, match='another UID'):
        gate.process_status(root, {'pid': 123}, proc)
