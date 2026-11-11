"""Package the frozen Linux ELF in a reproducible OCI image; no Docker daemon.

No source compilation, RF files, network access, or cluster mutation. The output
can be imported with `ctr -n k8s.io images import --digests <archive>`.
Specification: https://github.com/opencontainers/image-spec/blob/main/image-layout.md
"""
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import tarfile

EXPECTED = 'dbedbd61bb774a12c276f520d6fcdf990d262c8fb46cec57f59e098bede695fd'
MEDIA = 'application/vnd.oci.image.'


def digest(data):
    return 'sha256:' + hashlib.sha256(data).hexdigest()


def encoded(obj):
    return json.dumps(obj, sort_keys=True, separators=(',', ':')).encode()


def tar_bytes(files):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w', format=tarfile.USTAR_FORMAT) as archive:
        for name, data, mode in files:
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.uid, info.gid, info.mtime = len(data), mode, 0, 0, 0
            archive.addfile(info, io.BytesIO(data))
    return output.getvalue()


def build(binary, output, ref):
    data = binary.read_bytes()
    if hashlib.sha256(data).hexdigest() != EXPECTED or data[:4] != b'\x7fELF':
        raise ValueError('Not the exact frozen Stage4 Linux binary')
    output.mkdir(parents=True, exist_ok=False)
    layer_tar = tar_bytes([('vc-scheduler-hrrn', data, 0o555)])
    # GzipFile fixes both timestamp and filename, with deterministic OS byte.
    buf = io.BytesIO()
    with gzip.GzipFile(filename='', fileobj=buf, mode='wb', mtime=0, compresslevel=6) as stream:
        stream.write(layer_tar)
    layer = buf.getvalue()
    config = encoded(dict(architecture='amd64', os='linux',
        config=dict(User='1000:1000', Entrypoint=['/vc-scheduler-hrrn'], WorkingDir='/',
                    Labels={'gpu-lab.scheduler-binary-sha256': EXPECTED}),
        rootfs=dict(type='layers', diff_ids=[digest(layer_tar)])))
    def descriptor(content, media):
        return dict(mediaType=MEDIA+media, digest=digest(content), size=len(content))
    manifest = encoded(dict(schemaVersion=2, mediaType=MEDIA+'manifest.v1+json',
        config=descriptor(config, 'config.v1+json'), layers=[descriptor(layer, 'layer.v1.tar+gzip')]))
    desc = descriptor(manifest, 'manifest.v1+json')
    desc.update(platform=dict(architecture='amd64', os='linux'),
                annotations={'org.opencontainers.image.ref.name': ref})
    files = [('oci-layout', encoded(dict(imageLayoutVersion='1.0.0')), 0o644),
             ('index.json', encoded(dict(schemaVersion=2, mediaType=MEDIA+'index.v1+json', manifests=[desc])), 0o644)]
    for blob in (config, layer, manifest):
        files.append(('blobs/sha256/'+hashlib.sha256(blob).hexdigest(), blob, 0o644))
    archive = output/'scheduler.oci.tar'
    archive.write_bytes(tar_bytes(files))
    result = dict(binary_sha256=EXPECTED, manifest_digest=digest(manifest), config_digest=digest(config),
                  layer_digest=digest(layer), layer_diff_id=digest(layer_tar),
                  image_ref=ref, immutable_ref=ref.split(':')[0]+'@'+digest(manifest),
                  archive_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
                  archive_bytes=archive.stat().st_size, source='unchanged frozen Stage4 ELF',
                  cluster_imported=False)
    verify(archive)
    (output/'image.json').write_text(json.dumps(result, indent=2)+'\n')
    return result


def verify(path):
    with tarfile.open(path) as archive:
        def read(name):
            return archive.extractfile(name).read()
        def blob(desc):
            data = read('blobs/'+desc['digest'].replace(':', '/'))
            if digest(data) != desc['digest'] or len(data) != desc['size']:
                raise ValueError('OCI content integrity mismatch')
            return data
        index = json.loads(read('index.json'))
        manifest = json.loads(blob(index['manifests'][0]))
        config = json.loads(blob(manifest['config']))
        unpacked = gzip.decompress(blob(manifest['layers'][0]))
        if config['rootfs']['diff_ids'] != [digest(unpacked)]:
            raise ValueError('Uncompressed layer digest mismatch')
        with tarfile.open(fileobj=io.BytesIO(unpacked)) as layer:
            if layer.getnames() != ['vc-scheduler-hrrn']:
                raise ValueError('Unexpected image files')
            if hashlib.sha256(layer.extractfile('vc-scheduler-hrrn').read()).hexdigest() != EXPECTED:
                raise ValueError('Image ELF changed')
            if layer.getmember('vc-scheduler-hrrn').mode != 0o555:
                raise ValueError('Image ELF not executable')


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--binary', type=Path, default=Path('results/hrrn-native-stage4-20261026/bin/vc-scheduler-hrrn'))
    ap.add_argument('--output', type=Path, default=Path('results/hrrn-k8s-stage5-20261027/image'))
    ap.add_argument('--ref', default='docker.io/gpu-lab/volcano-hrrn:stage5-dbedbd61')
    a = ap.parse_args()
    print(json.dumps(build(a.binary, a.output, a.ref), indent=2))
