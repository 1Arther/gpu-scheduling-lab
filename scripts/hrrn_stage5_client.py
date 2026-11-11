"""Local SSH client for the installed, fixed-command Stage5 gate.

No password is read or logged. Each invocation preserves raw stdout/stderr,
including binary evidence export. This script does not install the remote key.
"""
import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def call(verb):
    stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    out = ROOT / 'results/hrrn-k8s-stage5-20261027/execution-live01/remote' / (stamp + '-' + verb)
    out.mkdir(parents=True, exist_ok=False)
    args = ['ssh.exe', '-T', '-i', str(Path.home() / '.ssh/id_ed25519_hrrn_stage5_execution_20261027'),
            '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
            '-o', 'UserKnownHostsFile=' + str(ROOT.parent / 'server_known_hosts'),
            '-o', 'ConnectTimeout=60', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3',
            'zbs@172.16.10.33', 'hrrn-stage5 ' + verb]
    start = dt.datetime.now(dt.timezone.utc).isoformat()
    # Evidence exports include full kubelet receipts and logs; a relayed
    # connection can take substantially longer than the read-only operations.
    timeout = 1800 if verb == 'export' else 600 if verb in ('prepare', 'recover') else 180
    try:
        p = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout)
        code, stdout, stderr = p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired as exc:
        code, stdout, stderr = 124, exc.stdout or b'', exc.stderr or b''
    # Export can be truncated on error: never imply a verified archive from rc alone.
    name = 'evidence.tar.gz' if verb == 'export' and code == 0 else 'stdout.bin'
    (out / name).write_bytes(stdout)
    (out / 'stderr.txt').write_bytes(stderr)
    meta = dict(start_utc=start, end_utc=dt.datetime.now(dt.timezone.utc).isoformat(),
                argv=args, exit_code=code, timeout_seconds=timeout,
                stdout_bytes=len(stdout), stdout_sha256=hashlib.sha256(stdout).hexdigest(),
                output_file=str(out / name))
    (out / 'metadata.json').write_text(json.dumps(meta, indent=2), encoding='utf-8')
    if verb != 'export':
        try:
            parsed = json.loads(stdout)
            (out / 'response.json').write_text(json.dumps(parsed, indent=2), encoding='utf-8')
        except (ValueError, UnicodeDecodeError):
            pass
    print(json.dumps(meta))
    if code:
        print(stderr.decode('utf-8', 'replace')[-2000:])
    return code


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('verb', choices=('prepare', 'start', 'status', 'stop', 'recover', 'export'))
    raise SystemExit(call(ap.parse_args().verb))
