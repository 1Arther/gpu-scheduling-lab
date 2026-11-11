"""Human-run, persistent, command-restricted SSH access for HRRN diagnostics.

Run as bszh on either GPU worker. No sudoers change, scheduler change, workload mutation,
unrestricted shell, upload access, or expiry bypass for any other key.
"""
import datetime
import os
from pathlib import Path
import shlex
import tempfile

KEY = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIIqMmY7Agfj+szj3Ss27+rBpGe8le951MJnGitg15iAV gpu-lab-hrrn-readonly-20261027'

GATE = r'''import os,subprocess,sys
K=['sudo','-n','kubectl','--kubeconfig=/etc/kubernetes/admin.conf']
G=['-o','json','--request-timeout=15s']
commands={
 'hrrn-basic-ro identity':['hostname'],
 'hrrn-basic-ro gpu':['nvidia-smi','--query-gpu=index,uuid,name,memory.total,memory.used,memory.free,utilization.gpu','--format=csv,nounits'],
 'hrrn-basic-ro processes':['nvidia-smi','--query-compute-apps=gpu_uuid,pid,process_name,used_gpu_memory','--format=csv,nounits'],
 'hrrn-basic-ro services':['systemctl','show','kubelet','containerd','docker','-p','Id','-p','ActiveState','-p','SubState','--no-pager'],
 'hrrn-basic-ro cluster':K+['get','nodes,pods,podgroups.scheduling.volcano.sh','-A']+G,
 'hrrn-basic-ro controllers':K+['get','deployments,daemonsets','-A']+G,
 'hrrn-basic-ro containers':['sudo','-n','crictl','ps','-a','-o','json'],
}
command=commands.get(os.environ.get('SSH_ORIGINAL_COMMAND',''))
if command is None:
 print('Denied: fixed HRRN basic read-only checks only',file=sys.stderr);sys.exit(126)
try:
 sys.exit(subprocess.run(command,stdin=subprocess.DEVNULL,timeout=40,env={'PATH':'/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'}).returncode)
except (OSError,subprocess.TimeoutExpired) as e:
 print(str(e),file=sys.stderr);sys.exit(125)
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
    auth, gate = ssh / 'authorized_keys', ssh / 'gpu-lab-hrrn-basic-worker-readonly.py'
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
    print('HRRN_BASIC_WORKER_READONLY_READY')
    print('Backup:', backup)
    print('Gate:', gate)
    print('Only fixed read-only checks; no interactive shell, file transfer, forwarding, or workload changes.')


if __name__ == '__main__':
    import pwd
    user = pwd.getpwuid(os.getuid())
    if user.pw_name != 'bszh':
        raise SystemExit('Run as bszh, not root.')
    os.umask(0o077)
    install(Path(user.pw_dir))
