"""Render the existing initial-4090 manifests locally. Never calls Kubernetes.

An immutable image reference is required; this does not approve a submission
window or prove scheduler quiescence. Admission policies and DQN patches are
deliberately outside the default bundle.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re

import yaml

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'k8s/round7-logical-window'
FILES = ['00-namespaces.yaml', '01-rbac.yaml', '02-queue-quota.yaml',
         '03-configmaps.yaml', '04-scheduler-DRAFT.yaml']


def immutable_image(value):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9./:_-]*@sha256:[0-9a-f]{64}', value):
        raise ValueError('Supply an actual immutable repository@sha256:<64 lowercase hex> image reference')
    return value


def render(scheduler_image, rf_image):
    images = {'scheduler': immutable_image(scheduler_image), 'rf-model': immutable_image(rf_image)}
    objects = []
    for name in FILES:
        objects.extend(yaml.safe_load_all((SOURCE / name).read_text(encoding='utf-8')))
    dep = next(o for o in objects if o['kind'] == 'Deployment')
    assert dep['metadata']['name'] == 'volcano-rf-test' and dep['spec']['replicas'] == 0
    for c in dep['spec']['template']['spec']['containers']:
        c['image'] = images[c['name']]
        c['imagePullPolicy'] = 'Never'  # Must already be imported under the exact reference on t3dgq.
    return objects


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--scheduler-image', required=True)
    p.add_argument('--rf-image', required=True)
    p.add_argument('--output-dir', type=Path, required=True)
    a = p.parse_args()
    objects = render(a.scheduler_image, a.rf_image)
    a.output_dir.mkdir(parents=True, exist_ok=False)
    manifest = a.output_dir / 'control-plane.yaml'
    manifest.write_text(yaml.safe_dump_all(objects, allow_unicode=True, sort_keys=False), encoding='utf-8')
    tests = ['test-a-training.yaml', 'test-b-inference.yaml', 'test-d-after-release.yaml',
             'test-e-control.yaml', 'test-f-fallback.yaml']
    for name in tests:
        (a.output_dir / name).write_bytes((SOURCE / name).read_bytes())
    record = dict(execution_authorized=False, replicas=0, target_node='t3dgq',
                  cluster_writes_performed=False, requires_verified_node_image_cache=True,
                  scheduler_image=a.scheduler_image, rf_image=a.rf_image,
                  admission_policy_included=False, dqn_changes_included=False,
                  source_sha256={f: hashlib.sha256((SOURCE/f).read_bytes()).hexdigest() for f in FILES + tests},
                  missing_operational_requirements=['approved coordinated window or separately approved enforced boundary',
                      'reconciled existing/private reservations', 'node owner startup consent', 'explicit cluster-write approval'])
    (a.output_dir / 'render-record.json').write_text(json.dumps(record, indent=2), encoding='utf-8')
    print(json.dumps(record, indent=2))


if __name__ == '__main__':
    main()
