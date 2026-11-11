"""Forced SSH command for a bounded read-only Stage5 preflight.

Install by the human operator under ~/.ssh, never as a root login key.
No client-provided command or argument is forwarded to a subprocess.
"""
import json
import os
import subprocess
import sys

K = ['sudo', '-n', 'kubectl', '--kubeconfig=/etc/kubernetes/admin.conf']
CHECKS = {
    'identity': ['id'],
    'nodes': K + ['get', 'nodes', '-o', 'json', '--request-timeout=15s'],
    'pods': K + ['get', 'pods', '-A', '-o', 'json', '--request-timeout=15s'],
    'volcano_jobs': K + ['get', 'jobs.batch.volcano.sh', '-A', '-o', 'json', '--request-timeout=15s'],
    'podgroups': K + ['get', 'podgroups.scheduling.volcano.sh', '-A', '-o', 'json', '--request-timeout=15s'],
    'queues': K + ['get', 'queues.scheduling.volcano.sh', '-o', 'json', '--request-timeout=15s'],
    'original_scheduler': K + ['get', 'deployment/volcano-scheduler', '-n', 'volcano-system', '-o', 'json', '--request-timeout=15s'],
    'dqn_service': K + ['get', 'deployment/dqn-scheduler-grpc', '-n', 'volcano-system', '-o', 'json', '--request-timeout=15s'],
    'rf_scheduler': K + ['get', 'deployment/volcano-rf-test', '-n', 'gpu-rf-system', '-o', 'json', '--request-timeout=15s'],
    'original_config': K + ['get', 'configmap/volcano-scheduler-configmap', '-n', 'volcano-system', '-o', 'json', '--request-timeout=15s'],
    'rf_config': K + ['get', 'configmap/rf-v7-config-on', '-n', 'gpu-rf-system', '-o', 'json', '--request-timeout=15s'],
    'quota': K + ['get', 'resourcequota/rf-v7-test-budget', '-n', 'gpu-rf-tests', '-o', 'json', '--request-timeout=15s'],
    'cached_images': ['sudo', '-n', 'crictl', 'images', '-o', 'json'],
}


def main():
    if os.environ.get('SSH_ORIGINAL_COMMAND') != 'hrrn-readonly-preflight-v1':
        print('Denied: this key only permits hrrn-readonly-preflight-v1', file=sys.stderr)
        return 126
    for name, command in CHECKS.items():
        try:
            r = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                               text=True, timeout=25, env={'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'})
            row = dict(check=name, exit_code=r.returncode, stdout=r.stdout, stderr=r.stderr)
        except (OSError, subprocess.TimeoutExpired) as exc:
            row = dict(check=name, error=str(exc))
        print(json.dumps(row), flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
