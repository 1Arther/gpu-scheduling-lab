"""Mocked containerd/CRI image alias checks; never contact a real runtime."""
import importlib.util
import json
from pathlib import Path
import sys
import types
from unittest.mock import Mock, patch

import pytest

from gpu_lab import staging

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
imports = {'staging': staging}
try:
    import fcntl  # noqa: F401
except ImportError:
    imports['fcntl'] = types.SimpleNamespace(LOCK_EX=2, LOCK_NB=4, LOCK_UN=8, flock=Mock())
with patch.dict(sys.modules, imports):
    spec = importlib.util.spec_from_file_location('stage5_image_alias_tested', ROOT / 'scripts/hrrn_stage5_live.py')
    live = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(live)

SOURCE = 'docker.io/gpu-lab/volcano-hrrn:stage5-dbedbd61'
MANIFEST = 'sha256:c03dae667ffc821f6ada20373e00434f1ec8d3f06a1c8c9ebba5c0759bef7947'
CONFIG = 'sha256:528d21f63c21e7a6804daa145df47f50544f6e20c1c897ff52f88c95f32b724a'
TARGET = 'docker.io/gpu-lab/volcano-hrrn@' + MANIFEST
MEDIA = 'application/vnd.oci.image.manifest.v1+json'
IMAGE = dict(image_ref=SOURCE, immutable_ref=TARGET, manifest_digest=MANIFEST,
             config_digest=CONFIG, binary_sha256=live.HRRN_SHA)
CONFIG_TEXT = '{"architecture":"amd64","config":{"Entrypoint":["/vc-scheduler-hrrn"],"Labels":{"gpu-lab.scheduler-binary-sha256":"dbedbd61bb774a12c276f520d6fcdf990d262c8fb46cec57f59e098bede695fd"},"User":"1000:1000","WorkingDir":"/"},"os":"linux","rootfs":{"diff_ids":["sha256:e7c7e81552987bbbe4b59ce68e8d58c303de82e72de975655bd56213d3671938"],"type":"layers"}}'
MANIFEST_TEXT = '{"config":{"digest":"sha256:528d21f63c21e7a6804daa145df47f50544f6e20c1c897ff52f88c95f32b724a","mediaType":"application/vnd.oci.image.config.v1+json","size":346},"layers":[{"digest":"sha256:6fe6434b5290adc740908f190f70686fa9892c1b9d25a556ff0a8e48bb128811","mediaType":"application/vnd.oci.image.layer.v1.tar+gzip","size":42538983}],"mediaType":"application/vnd.oci.image.manifest.v1+json","schemaVersion":2}'
CTR = ['sudo', '-n', 'ctr', '-n', 'k8s.io']


class Runtime:
    def __init__(self):
        self.refs = {SOURCE: (MEDIA, MANIFEST)}
        self.blobs = {MANIFEST: MANIFEST_TEXT, CONFIG: CONFIG_TEXT}
        self.config_ids = {SOURCE: CONFIG, TARGET: CONFIG}
        self.not_found = {SOURCE: 0, TARGET: 0}
        self.digest_delays = 0
        self.tag_exit = 0
        self.tag_creates = True
        self.corrupt_on_tag = False
        self.calls = []
        self.before_tag = None

    def command(self, args, **kwargs):
        self.calls.append(args)
        result = types.SimpleNamespace(stdout='', stderr='', returncode=0)
        if args == CTR + ['images', 'list']:
            result.stdout = 'REF TYPE DIGEST SIZE PLATFORMS LABELS\n' + '\n'.join(
                f'{name} {kind} {digest} 40MiB linux/amd64 -' for name, (kind, digest) in self.refs.items())
        elif args[:7] == CTR + ['content', 'get']:
            result.stdout = self.blobs[args[7]]
        elif args[:8] == CTR + ['images', 'tag', '--local']:
            assert args[8:] == [SOURCE, TARGET]
            assert '--force' not in args
            if self.before_tag:
                self.before_tag()
            result.returncode = self.tag_exit
            if self.tag_creates and TARGET not in self.refs:
                self.refs[TARGET] = (MEDIA, 'sha256:wrong') if self.corrupt_on_tag else self.refs[SOURCE]
        elif args[:6] == ['sudo', '-n', 'crictl', 'inspecti', '-o', 'json']:
            ref = args[6]
            if self.not_found[ref] > 0:
                self.not_found[ref] -= 1
                result.returncode = 1
                result.stderr = 'no such image'
            else:
                digests = [TARGET] if TARGET in self.refs else []
                if ref == TARGET and self.digest_delays:
                    self.digest_delays -= 1
                    digests = []
                result.stdout = json.dumps({'status': {'id': self.config_ids[ref],
                    'repoTags': [SOURCE], 'repoDigests': digests}})
        else:
            raise AssertionError('Unexpected command: ' + str(args))
        return result

    @property
    def mutations(self):
        return [c for c in self.calls if c[:7] == CTR + ['images', 'tag']]


@pytest.fixture
def runner(tmp_path, monkeypatch):
    run = live.Run.__new__(live.Run)
    run.out = tmp_path
    run.guard = Mock()
    run.event = Mock()
    runtime = Runtime()
    run.cmd = runtime.command
    tick = [0]
    monkeypatch.setattr(live, 'time', types.SimpleNamespace(
        monotonic=lambda: tick[0], sleep=lambda seconds: tick.__setitem__(0, tick[0] + seconds)))
    monkeypatch.setattr(live.subprocess, 'run', Mock(side_effect=AssertionError('Real subprocess forbidden')))
    return run, runtime


def test_missing_alias_is_created_once_without_force(runner):
    run, runtime = runner
    result = run.ensure_immutable_image(IMAGE)
    assert runtime.mutations == [CTR + ['images', 'tag', '--local', SOURCE, TARGET]]
    assert result['source_cri']['id'] == result['target_cri']['id'] == CONFIG
    assert TARGET in result['target_cri']['repoDigests']
    assert result['force_used'] is False and result['existing_images_deleted'] is False
    assert json.loads((run.out / 'image-alias-proof.json').read_text())['target'] == TARGET
    assert runtime.calls.index(['sudo', '-n', 'crictl', 'inspecti', '-o', 'json', SOURCE]) < runtime.calls.index(runtime.mutations[0])


def test_correct_existing_alias_is_only_verified(runner):
    run, runtime = runner
    runtime.refs[TARGET] = (MEDIA, MANIFEST)
    assert run.ensure_immutable_image(IMAGE)['alias_requested'] is False
    assert runtime.mutations == []


@pytest.mark.parametrize('field', list(IMAGE))
def test_alias_method_cannot_target_another_image(runner, field):
    run, runtime = runner
    image = {**IMAGE, field: 'unapproved'}
    with pytest.raises(RuntimeError, match='outside the exact frozen'):
        run.ensure_immutable_image(image)
    assert runtime.calls == []


@pytest.mark.parametrize('ref', [SOURCE, TARGET])
def test_mismatching_existing_manifest_is_never_overwritten(runner, ref):
    run, runtime = runner
    runtime.refs[ref] = (MEDIA, 'sha256:wrong')
    with pytest.raises(RuntimeError, match='manifest differs'):
        run.ensure_immutable_image(IMAGE)
    assert runtime.mutations == []


def test_missing_source_fails_before_mutation(runner):
    run, runtime = runner
    runtime.refs.clear()
    with pytest.raises(RuntimeError, match='reference is missing'):
        run.ensure_immutable_image(IMAGE)
    assert runtime.mutations == []


@pytest.mark.parametrize('digest', [MANIFEST, CONFIG])
def test_corrupt_runtime_content_fails_before_mutation(runner, digest):
    run, runtime = runner
    runtime.blobs[digest] += '\n'
    with pytest.raises(RuntimeError, match='content digest mismatch'):
        run.ensure_immutable_image(IMAGE)
    assert runtime.mutations == []


def test_wrong_source_cri_configuration_fails_before_tag(runner):
    run, runtime = runner
    runtime.config_ids[SOURCE] = 'sha256:other'
    with pytest.raises(RuntimeError, match='CRI configuration ID differs'):
        run.ensure_immutable_image(IMAGE)
    assert runtime.mutations == []


def test_wrong_target_cri_configuration_is_not_accepted(runner):
    run, runtime = runner
    runtime.config_ids[TARGET] = 'sha256:other'
    with pytest.raises(RuntimeError, match='CRI configuration ID differs'):
        run.ensure_immutable_image(IMAGE)
    assert len(runtime.mutations) == 1
    assert not (run.out / 'image-alias-proof.json').exists()


def test_bounded_read_only_retries_allow_cri_event_propagation(runner):
    run, runtime = runner
    runtime.not_found[SOURCE] = 1
    runtime.not_found[TARGET] = 1
    runtime.digest_delays = 2
    assert run.ensure_immutable_image(IMAGE)['target_cri']['repoDigests'] == [TARGET]
    assert len(runtime.mutations) == 1
    assert len([c for c in runtime.calls if c[-1] == TARGET and 'inspecti' in c]) == 4


def test_cri_alias_never_appearing_times_out_without_retagging(runner):
    run, runtime = runner
    runtime.digest_delays = 1000
    with pytest.raises(RuntimeError, match='Timed out waiting'):
        run.ensure_immutable_image(IMAGE)
    assert len(runtime.mutations) == 1
    assert 1 < len([c for c in runtime.calls if c[-1] == TARGET and 'inspecti' in c]) <= 18


def test_concurrent_correct_alias_is_verified_after_nonzero_create(runner):
    run, runtime = runner
    runtime.tag_exit = 1
    runtime.before_tag = lambda: runtime.refs.update({TARGET: (MEDIA, MANIFEST)})
    assert run.ensure_immutable_image(IMAGE)['tag_exit_code'] == 1
    assert len(runtime.mutations) == 1


def test_concurrent_wrong_alias_is_rejected_without_replacement(runner):
    run, runtime = runner
    runtime.tag_exit = 1
    runtime.before_tag = lambda: runtime.refs.update({TARGET: (MEDIA, 'sha256:foreign')})
    with pytest.raises(RuntimeError, match='manifest differs'):
        run.ensure_immutable_image(IMAGE)
    assert runtime.refs[TARGET][1] == 'sha256:foreign'
    assert len(runtime.mutations) == 1


def test_tag_failure_does_not_reimport_or_force(runner):
    run, runtime = runner
    runtime.tag_exit = 1
    runtime.tag_creates = False
    with pytest.raises(RuntimeError, match='reference is missing'):
        run.ensure_immutable_image(IMAGE)
    assert runtime.mutations == [CTR + ['images', 'tag', '--local', SOURCE, TARGET]]


def test_source_change_at_tag_is_caught_by_post_tag_manifest_check(runner):
    run, runtime = runner
    runtime.before_tag = lambda: runtime.refs.update({SOURCE: (MEDIA, 'sha256:changed')})
    with pytest.raises(RuntimeError, match='manifest differs'):
        run.ensure_immutable_image(IMAGE)
    assert not (run.out / 'image-alias-proof.json').exists()


def test_operator_guard_prevents_alias_mutation(runner):
    run, runtime = runner
    run.guard.side_effect = RuntimeError('Competing GPU request')
    with pytest.raises(RuntimeError, match='Competing GPU'):
        run.ensure_immutable_image(IMAGE)
    assert runtime.mutations == []
