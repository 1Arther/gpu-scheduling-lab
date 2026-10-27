"""Strict-key SSH transport for the isolated CPU verification directory (no kubectl)."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--command', required=True)
    ap.add_argument('--input-file')
    ap.add_argument('--output-file')
    ap.add_argument('--timeout', type=int, default=900)
    ap.add_argument('--target', default='zbs@172.16.10.33',
                    choices=['zbs@172.16.10.33','bszh@172.16.20.34','bszh@172.16.10.34'])
    a = ap.parse_args()
    project = Path(__file__).resolve().parents[1]
    if not os.environ.get('GPU_LAB_SSH_PASSWORD'):
        raise SystemExit('Set GPU_LAB_SSH_PASSWORD; credentials are never saved')
    with tempfile.TemporaryDirectory(prefix='rf-askpass-') as tmp:
        helper = Path(tmp) / 'answer.py'
        helper.write_text('import os,sys\nsys.stdout.write(os.environ["GPU_LAB_SSH_PASSWORD"])\n')
        ask = Path(tmp) / 'answer.cmd'
        ask.write_text('@echo off\r\n"' + getattr(sys, '_base_executable', sys.executable) + '" "' + str(helper) + '"\r\n')
        env = os.environ.copy()
        env.update(SSH_ASKPASS=str(ask), SSH_ASKPASS_REQUIRE='force', DISPLAY='rf-verify')
        command = ['ssh', '-T', '-o', 'StrictHostKeyChecking=yes', '-o',
                   'UserKnownHostsFile=' + str(project.parent / 'server_known_hosts'),
                   '-o', 'ConnectTimeout=30', '-o', 'NumberOfPasswordPrompts=1',
                   '-o', 'PreferredAuthentications=password', '-o', 'PubkeyAuthentication=no',
                   '-o', 'ServerAliveInterval=30', '-o', 'ServerAliveCountMax=6',
                   a.target, a.command]
        payload = Path(a.input_file).read_bytes() if a.input_file else b''
        if a.output_file:
            with open(a.output_file, 'wb') as out:
                result = subprocess.run(command, input=payload, stdout=out, env=env, timeout=a.timeout)
        else:
            result = subprocess.run(command, input=payload, env=env, timeout=a.timeout)
        raise SystemExit(result.returncode)


if __name__ == '__main__':
    main()
