"""Fixed-command, one-attempt SSH gate for the reviewed HRRN A/B experiment."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tarfile
import time

ROOT = Path(__file__).resolve().parent
FILES = frozenset({
    'hrrn_stage5_gate.py', 'hrrn_stage5_live.py', 'hrrn_stage5_evidence.py',
    'protocol.json', 'scheduler.oci.tar', 'image.json', 'helpers/staging.py',
    'helpers/round7_collect.py', 'helpers/round7_window_preflight.py',
    'gpu_lab/__init__.py', 'gpu_lab/staging.py',
})
HASHES = {
    'production_scheduler': '9f2e1ae3359ce234fd048bc48aab082add99a686631808908b5ae518bdebf4f4',
    'hrrn_scheduler': 'dbedbd61bb774a12c276f520d6fcdf990d262c8fb46cec57f59e098bede695fd',
    'rf_model': 'ee35cfabe30fffc28ff0be09d3738587d4f9bffbe26386e2a9fae5c5b9e80748',
}
ENV = {'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
       'HOME': '/home/zbs', 'USER': 'zbs', 'LOGNAME': 'zbs',
       'LANG': 'C.UTF-8', 'PYTHONUNBUFFERED': '1'}
IMAGE_ARCHIVE_SHA = '13d0ccf9e0710ee77fd06efeaa8d01efec80f1dbb86887347b9cb2607c0e3d4a'
IMAGE_MANIFEST_DIGEST = 'sha256:c03dae667ffc821f6ada20373e00434f1ec8d3f06a1c8c9ebba5c0759bef7947'
VERBS = {f'hrrn-stage5 {v}': v for v in ('prepare', 'start', 'status', 'stop', 'recover', 'export')}


def relative_name(name):
    p = PurePosixPath(name)
    if not name or p.is_absolute() or '\\' in name or ':' in name or any(x in ('', '.', '..') for x in name.split('/')):
        raise ValueError('Invalid package/export member path')
    return p


def safe_path(root, relative):
    p = relative_name(relative)
    root = root.resolve()
    current = root
    for part in p.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError('Symlink in experiment path')
    if not current.resolve().is_relative_to(root):
        raise ValueError('Path escapes experiment root')
    return current


def identity_policy(username, uid, root_uid):
    if username != 'zbs' or uid == 0 or uid != root_uid:
        raise PermissionError('Only the owning zbs account may use this gate; never root')


def require_identity(root):
    import pwd  # Linux-only runtime, not an import-time dependency for tests.
    user = pwd.getpwuid(os.getuid())
    identity_policy(user.pw_name, user.pw_uid, root.stat().st_uid)


def validate_protocol(protocol):
    if protocol.get('live_scope') != ['order-a', 'order-b']:
        raise ValueError('Only the reviewed first A/B tranche is allowed')
    enabled = [c for c in protocol['cases'] if c.get('execution_enabled')]
    if [(c['id'], c['mode'], c['kind']) for c in enabled] != [('order-a', 'A', 'order'), ('order-b', 'B', 'order')]:
        raise ValueError('Complete-Job and other unreviewed cases are disabled')
    if protocol.get('propagation', {}).get('execution_enabled'):
        raise ValueError('Metadata propagation is prepared, not enabled in this tranche')


def verify_bundle(root):
    manifest = json.loads(safe_path(root, 'bundle-manifest.json').read_text())
    if set(manifest.get('files', {})) != FILES or manifest.get('frozen_shas') != HASHES:
        raise ValueError('Unexpected approved bundle scope')
    for name, expected in manifest['files'].items():
        path = safe_path(root, name)
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError('Immutable bundle integrity failed: ' + name)
    protocol = json.loads(safe_path(root, 'protocol.json').read_text())
    validate_protocol(protocol)
    image = json.loads(safe_path(root, 'image.json').read_text())
    if (image['binary_sha256'] != HASHES['hrrn_scheduler'] or image['archive_sha256'] != IMAGE_ARCHIVE_SHA
            or image['manifest_digest'] != IMAGE_MANIFEST_DIGEST
            or manifest['files']['scheduler.oci.tar'] != IMAGE_ARCHIVE_SHA):
        raise ValueError('Frozen image identity changed')
    return manifest


def save(path, value):
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, indent=2) + '\n')
    os.replace(temp, path)


def process_status(root, record, proc_root=Path('/proc')):
    if not record or not record.get('pid'):
        return {'alive': False, 'reason': 'no_recorded_pid'}
    pid = int(record['pid'])
    if pid <= 1:
        raise ValueError('Invalid recorded runner PID')
    path = proc_root / str(pid)
    if not path.exists():
        return {'alive': False, 'reason': 'process_exited', 'pid': pid}
    if path.stat().st_uid != root.stat().st_uid:
        raise PermissionError('Recorded PID belongs to another UID; no signal or recovery dispatch')
    args = (path / 'cmdline').read_bytes().split(b'\0')
    fields = (path / 'stat').read_text().rsplit(')', 1)[1].split()
    if fields[0] == 'Z':
        return {'alive': False, 'reason': 'zombie', 'pid': pid}
    if str(root / 'hrrn_stage5_live.py').encode() not in args or b'--execute' not in args:
        raise RuntimeError('Recorded PID no longer identifies this runner')
    ticks = fields[19]  # /proc/PID/stat field22, after removing PID + (comm).
    if record.get('start_ticks') and record['start_ticks'] != ticks:
        raise RuntimeError('Recorded PID was reused')
    return {'alive': True, 'pid': pid, 'start_ticks': ticks}


class Gate:
    def __init__(self, root=ROOT):
        self.root = root.resolve()
        self.out = safe_path(self.root, 'evidence')
        self.out.mkdir(exist_ok=True)
        self.launch = safe_path(self.root, 'evidence/launch.json')

    def state(self):
        path = safe_path(self.root, 'evidence/state.json')
        return json.loads(path.read_text()) if path.exists() else {'phase': 'new'}

    def runner(self):
        record = json.loads(self.launch.read_text()) if self.launch.exists() else None
        return process_status(self.root, record)

    def foreground(self, flag):
        result = subprocess.run([sys.executable, str(self.root / 'hrrn_stage5_live.py'), flag],
                                cwd=self.root, stdin=subprocess.DEVNULL, env=ENV)
        return result.returncode

    def prepare(self):
        verify_bundle(self.root)
        if self.launch.exists():
            raise RuntimeError('A launch was already attempted; preserve this attempt')
        return self.foreground('--prepare')

    def start(self):
        verify_bundle(self.root)
        if self.state().get('phase') != 'prepared':
            raise RuntimeError('Only a successfully prepared attempt may start')
        if safe_path(self.root, 'STOP').exists():
            raise RuntimeError('STOP is present; never clear it automatically')
        # Durable, exclusive intent before launching. Even a failed Popen consumes
        # this attempt so another SSH request cannot create a duplicate runner.
        record = {'attempted_epoch': time.time(), 'pid': None, 'status': 'launch_intent'}
        with self.launch.open('x') as stream:
            json.dump(record, stream)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            with safe_path(self.root, 'evidence/runner.log').open('ab') as log:
                process = subprocess.Popen([sys.executable, str(self.root / 'hrrn_stage5_live.py'), '--execute'],
                    cwd=self.root, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                    start_new_session=True, env=ENV)
            record.update(pid=process.pid, status='launched')
            save(self.launch, record)
            try:
                observed = process_status(self.root, record)
                if observed.get('start_ticks'):
                    record['start_ticks'] = observed['start_ticks']
                    save(self.launch, record)
            except FileNotFoundError:
                record['status'] = 'exited_during_startup'
                save(self.launch, record)
            print(json.dumps(record))
            return 0
        except BaseException as exc:
            record.update(status='launch_failed_or_uncertain', error=repr(exc))
            save(self.launch, record)
            raise

    def status(self):
        events = safe_path(self.root, 'evidence/events.jsonl')
        recent = []
        if events.exists():
            with events.open('rb') as stream:
                stream.seek(max(0, events.stat().st_size - 32768))
                lines = stream.read().decode('utf-8', 'replace').splitlines()
            for line in lines[-20:]:
                try:
                    recent.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        print(json.dumps({'state': self.state(), 'runner': self.runner(), 'stop_requested': safe_path(self.root, 'STOP').exists(), 'recent_events': recent}))
        return 0

    def stop(self):
        safe_path(self.root, 'STOP').touch()
        print(json.dumps({'stop_requested': True, 'action': 'Only this attempt STOP file; no process signal or foreign workload change'}))
        return 0

    def recover(self):
        if self.runner()['alive']:
            return self.stop()
        verify_bundle(self.root)
        return self.foreground('--recover')

    def export(self, output):
        # Only evidence and protocol. Never package authorized_keys, credentials,
        # other home files, the image archive, or arbitrary client-selected paths.
        files = [safe_path(self.root, 'protocol.json')]
        for path in sorted(self.out.rglob('*')):
            relative = path.relative_to(self.root).as_posix()
            checked = safe_path(self.root, relative)
            if checked.is_file():
                files.append(checked)
        total = sum(path.stat().st_size for path in files)
        if total > 1024 * 1024 * 1024:
            raise RuntimeError('Evidence exceeds bounded 1 GiB export limit')
        with tarfile.open(fileobj=output, mode='w|gz') as archive:
            for path in files:
                info = archive.gettarinfo(str(path), path.relative_to(self.root).as_posix())
                if not info.isreg():
                    raise ValueError('Only regular evidence files may be exported')
                with path.open('rb') as stream:
                    archive.addfile(info, stream)
        return 0


def dispatch(command, root=ROOT, output=None):
    if command not in VERBS:
        raise ValueError('Denied: exact Stage5 prepare/start/status/stop/recover/export commands only')
    require_identity(root)
    gate = Gate(root)
    verb = VERBS[command]
    if verb == 'export':
        return gate.export(sys.stdout.buffer if output is None else output)
    return getattr(gate, verb)()


if __name__ == '__main__':
    os.umask(0o077)
    try:
        sys.exit(dispatch(os.environ.get('SSH_ORIGINAL_COMMAND', '')))
    except Exception as exc:
        print(json.dumps({'error': type(exc).__name__, 'message': str(exc)}), file=sys.stderr)
        sys.exit(126)
