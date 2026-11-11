"""Bundle only the fixed, already-produced r4 evidence; no cluster commands."""

import hashlib
import os
from pathlib import Path
import stat
import sys
import tarfile

ROOT = Path('/home/zbs/hrrn-stage5-20261027-live01-r4')
OUTPUT = Path('/home/zbs/hrrn-stage5-r4-tail-20261027.xz.tar')
FILES = (
    'protocol.json',
    'evidence/state.json',
    'evidence/restore-confirmation.json',
    'evidence/pod-observations.jsonl',
    'evidence/logs/release-a7c0770c-d90e-4182-8611-08115191ea44-scheduler.log',
    'evidence/logs/release-a7c0770c-d90e-4182-8611-08115191ea44-rf-model.log',
)
DIRECTORIES = ('evidence/proofs', 'evidence/snapshots')


def _safe_path(root, relative):
    parts = Path(relative).parts
    if not parts or Path(relative).is_absolute() or any(x in ('', '.', '..') for x in parts):
        raise ValueError('Unsafe relative evidence path: ' + str(relative))
    current = root
    for part in parts:
        current = current / part
        info = current.lstat()  # Missing or symlink is never silently ignored.
        if stat.S_ISLNK(info.st_mode):
            raise ValueError('Symlink in evidence path: ' + str(current))
    if not current.resolve(strict=True).is_relative_to(root):
        raise ValueError('Evidence path escapes fixed root: ' + str(current))
    return current


def _members(root):
    """Validate every required file and directory before creating the archive."""
    found = {}
    for relative in FILES:
        path = _safe_path(root, relative)
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('Required evidence is not a regular file: ' + str(path))
        found[relative] = (path, info)
    for relative in DIRECTORIES:
        base = _safe_path(root, relative)
        if not base.is_dir():
            raise ValueError('Required evidence directory missing: ' + str(base))
        for parent, dirs, files in os.walk(base, followlinks=False):
            for name in sorted(dirs + files):
                path = Path(parent) / name
                rel = path.relative_to(root).as_posix()
                checked = _safe_path(root, rel)
                info = checked.stat()
                if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                    raise ValueError('Unsupported evidence type: ' + str(checked))
                found[rel] = (checked, info)
        found[relative] = (base, base.stat())
    return found


def _identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def create_bundle(root, output):
    """root/output arguments support isolated tests; main uses fixed constants."""
    root, output = Path(root), Path(output)
    if root.is_symlink():
        raise ValueError('Experiment root must not be a symlink')
    root = root.resolve(strict=True)
    if not root.is_dir() or output.is_symlink() or output.exists():
        raise ValueError('Missing root or output already exists')
    if output.resolve(strict=False).is_relative_to(root):
        raise ValueError('Archive must not be placed within the evidence root')
    if not output.parent.is_dir() or output.parent.is_symlink():
        raise ValueError('Archive parent must be an existing non-symlink directory')
    members = _members(root)
    # Recheck all files, identity, and permissions before exclusive creation.
    for path, previous in members.values():
        current = path.lstat()
        if stat.S_ISLNK(current.st_mode) or _identity(current) != _identity(previous):
            raise ValueError('Evidence changed during validation: ' + str(path))
        if stat.S_ISREG(current.st_mode):
            fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
            try:
                if _identity(os.fstat(fd)) != _identity(previous):
                    raise ValueError('Evidence changed during open: ' + str(path))
            finally:
                os.close(fd)
    with output.open('xb') as stream:
        with tarfile.open(fileobj=stream, mode='w:xz', preset=1) as archive:
            for name, (path, previous) in sorted(members.items()):
                info = tarfile.TarInfo(name)
                info.mode, info.uid, info.gid, info.mtime = (stat.S_IMODE(previous.st_mode),
                    getattr(previous, 'st_uid', 0), getattr(previous, 'st_gid', 0), previous.st_mtime)
                if stat.S_ISDIR(previous.st_mode):
                    info.type = tarfile.DIRTYPE
                    archive.addfile(info)
                    continue
                fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
                with os.fdopen(fd, 'rb') as source:
                    if _identity(os.fstat(source.fileno())) != _identity(previous):
                        raise ValueError('Evidence changed before archiving: ' + str(path))
                    info.size = previous.st_size
                    archive.addfile(info, source)
                    if _identity(os.fstat(source.fileno())) != _identity(previous):
                        raise ValueError('Evidence changed while archiving: ' + str(path))
    digest = hashlib.sha256()
    with output.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return {'archive': str(output), 'bytes': output.stat().st_size,
            'sha256': digest.hexdigest(), 'members': len(members)}


if __name__ == '__main__':
    if len(sys.argv) != 1:
        raise SystemExit('This command accepts no arguments; all paths are fixed.')
    result = create_bundle(ROOT, OUTPUT)
    print('ARCHIVE:', result['archive'])
    print('BYTES:', result['bytes'])
    print('SHA256:', result['sha256'])
    print('MEMBERS:', result['members'])
