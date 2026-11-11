"""Human-run, persistent, command-restricted SSH access for HRRN diagnostics.

Run as zbs on the server. No sudoers change, scheduler change, workload mutation,
unrestricted shell, upload access, or expiry bypass for any other key.
"""
import datetime
import os
from pathlib import Path
import shlex
import tempfile

KEY = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIIqMmY7Agfj+szj3Ss27+rBpGe8le951MJnGitg15iAV gpu-lab-hrrn-readonly-20261027'

GATE = r'''import os
import subprocess
import sys

K = ['sudo', '-n', 'kubectl', '--kubeconfig=/etc/kubernetes/admin.conf']
GET = ['-o', 'json', '--request-timeout=15s']
commands = {
    'hrrn-readonly-preflight-v1': K + ['get', 'nodes,pods,jobs.batch.volcano.sh,podgroups.scheduling.volcano.sh,queues.scheduling.volcano.sh', '-A'] + GET,
    'hrrn-ro gpu': ['nvidia-smi'],
    'hrrn-ro controllers': K + ['get', 'deployments,statefulsets,daemonsets,replicasets,jobs.batch,cronjobs.batch', '-A'] + GET,
    'hrrn-ro claims': K + ['get', 'resourceclaims.resource.k8s.io', '-A'] + GET,
    'hrrn-ro quota': K + ['get', 'resourcequota/rf-v7-test-budget', '-n', 'gpu-rf-tests'] + GET,
    'hrrn-ro scheduler-config': K + ['get', 'configmap/volcano-scheduler-configmap', '-n', 'volcano-system'] + GET,
    'hrrn-ro rf-config': K + ['get', 'configmap/rf-v7-config-on', '-n', 'gpu-rf-system'] + GET,
    'hrrn-ro runtime': K + ['get', 'runtimeclass/nvidia'] + GET,
    'hrrn-ro scheduler-log': K + ['logs', '-n', 'volcano-system', 'deployment/volcano-scheduler', '--since=10m', '--timestamps', '--limit-bytes=2000000', '--request-timeout=15s'],
    'hrrn-ro images': ['sudo', '-n', 'crictl', 'images', '-o', 'json'],
    'hrrn-ro containers': ['sudo', '-n', 'crictl', 'ps', '-o', 'json'],
    'hrrn-ro scheduler-file-sha': ['sha256sum', '/home/zbs/volcano-v1.13.1-dqn/_output/bin/vc-scheduler'],
    'hrrn-ro rf-model-sha': ['sha256sum', '/home/zbs/rf-round7-live-window-20261019/lab/models/pod-gpu-rf-v1/random_forest.joblib'],
}

def main():
    command = commands.get(os.environ.get('SSH_ORIGINAL_COMMAND', ''))
    if command is None:
        print('Denied: only named HRRN read-only checks are permitted.', file=sys.stderr)
        return 126
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, timeout=100,
                                env={'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'})
        return result.returncode
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(str(exc), file=sys.stderr)
        return 125

if __name__ == '__main__':
    sys.exit(main())
'''


def replace_key(text, entry):
    # Preserve every unrelated line, including comments and other users' keys.
    blob = KEY.split()[1]
    lines = text.splitlines(keepends=True)
    kept = [line for line in lines if line.lstrip().startswith('#') or blob not in line.split()]
    result = ''.join(kept)
    if result and not result.endswith('\n'):
        result += '\n'
    return result + entry + '\n'


def install(home):
    ssh = home / '.ssh'
    if ssh.is_symlink():
        raise RuntimeError('Refuse symlinked .ssh directory; inspect manually.')
    ssh.mkdir(mode=0o700, exist_ok=True)
    auth, gate = ssh / 'authorized_keys', ssh / 'gpu-lab-hrrn-readonly.py'
    if auth.is_symlink() or gate.is_symlink():
        raise RuntimeError('Refuse symlinked access files; inspect manually.')
    original = auth.read_text() if auth.exists() else ''
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backup = ssh / ('authorized_keys.before-hrrn-readonly-' + stamp)
    with backup.open('x') as stream:
        stream.write(original)
    backup.chmod(0o600)
    compile(GATE, str(gate), 'exec')
    with tempfile.NamedTemporaryFile(mode='w', dir=ssh, prefix='.hrrn-gate-', delete=False) as stream:
        stream.write(GATE)
        new_gate = Path(stream.name)
    new_gate.chmod(0o500)
    os.replace(new_gate, gate)
    forced = '/usr/bin/python3 -I ' + shlex.quote(str(gate))
    forced = forced.replace('\\', '\\\\').replace('"', '\\"')
    entry = 'restrict,command="' + forced + '" ' + KEY
    # The user explicitly requested persistence: no expiry-time option. The
    # command restrictions still apply. No other key has its expiry changed.
    with tempfile.NamedTemporaryFile(mode='w', dir=ssh, prefix='.hrrn-auth-', delete=False) as stream:
        stream.write(replace_key(original, entry))
        temp = Path(stream.name)
    temp.chmod(0o600)
    os.replace(temp, auth)
    ssh.chmod(0o700)
    print('PERSISTENT_READONLY_READY')
    print('Backup:', backup)
    print('Gate:', gate)
    print('Only fixed read-only checks; no interactive shell, file transfer, forwarding, or workload changes.')


if __name__ == '__main__':
    import pwd
    user = pwd.getpwuid(os.getuid())
    if user.pw_name != 'zbs':
        raise SystemExit('Run as zbs, not root.')
    os.umask(0o077)
    install(Path(user.pw_dir))
