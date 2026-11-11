"""Read-only supplemental archive validation with no cluster access."""
import hashlib
import importlib.util
from pathlib import Path
import shutil
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('tail_bundle', ROOT / 'scripts/hrrn_stage5_r4_tail_bundle.py')
bundle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bundle)


class TailBundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / 'evidence-root'
        self.root.mkdir()
        self.output = self.base / 'bundle.xz.tar'
        for name in bundle.FILES:
            file = self.root / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(('synthetic ' + name + '\n').encode())
        for name in bundle.DIRECTORIES:
            path = self.root / name
            path.mkdir(parents=True, exist_ok=True)
            (path / 'nested').mkdir()
            (path / 'nested' / 'evidence.json').write_text('{"saved":true}\n')
        # Exercise real r3 saved logs using the r4 target names; never edit source.
        for kind in ('scheduler', 'rf-model'):
            source = ROOT / ('results/live-r3-a/evidence/logs/release-'
                             'b3bdf951-c672-4ba1-8fea-13c1666f348b-' + kind + '.log')
            target = self.root / ('evidence/logs/release-'
                                  'a7c0770c-d90e-4182-8611-08115191ea44-' + kind + '.log')
            shutil.copyfile(source, target)

    def tearDown(self):
        self.temp.cleanup()

    def test_archive_contains_exact_files_and_matching_contents(self):
        result = bundle.create_bundle(self.root, self.output)
        self.assertEqual(result['bytes'], self.output.stat().st_size)
        self.assertEqual(result['sha256'], hashlib.sha256(self.output.read_bytes()).hexdigest())
        with tarfile.open(self.output, 'r:xz') as archive:
            members = archive.getmembers()
            names = {m.name for m in members}
            self.assertEqual(len(names), result['members'])
            self.assertEqual(names, {p.relative_to(self.root).as_posix() for p in self.root.rglob('*')
                                     if p.is_file() or p.relative_to(self.root).as_posix() in bundle.DIRECTORIES
                                     or any(p.relative_to(self.root).as_posix().startswith(d + '/')
                                            for d in bundle.DIRECTORIES)})
            for member in members:
                self.assertFalse(member.issym() or member.islnk())
                if member.isfile():
                    self.assertEqual(archive.extractfile(member).read(), (self.root / member.name).read_bytes())

    def test_missing_required_file_or_directory_does_not_create_output(self):
        (self.root / bundle.FILES[0]).unlink()
        with self.assertRaises(FileNotFoundError):
            bundle.create_bundle(self.root, self.output)
        self.assertFalse(self.output.exists())
        (self.root / bundle.FILES[0]).write_text('restored')
        shutil.rmtree(self.root / bundle.DIRECTORIES[1])
        with self.assertRaises(FileNotFoundError):
            bundle.create_bundle(self.root, self.output)
        self.assertFalse(self.output.exists())

    def test_existing_output_is_never_overwritten(self):
        self.output.write_bytes(b'keep existing archive')
        with self.assertRaises(ValueError):
            bundle.create_bundle(self.root, self.output)
        self.assertEqual(self.output.read_bytes(), b'keep existing archive')

    def test_output_inside_evidence_is_forbidden(self):
        output = self.root / 'evidence' / 'new.xz.tar'
        with self.assertRaises(ValueError):
            bundle.create_bundle(self.root, output)
        self.assertFalse(output.exists())

    def test_rejects_nested_symlink_and_never_creates_archive(self):
        link = self.root / 'evidence/proofs/nested/link'
        try:
            link.symlink_to(self.base, target_is_directory=True)
        except OSError:
            self.skipTest('Creating symlinks is unavailable for this account')
        with self.assertRaises(ValueError):
            bundle.create_bundle(self.root, self.output)
        self.assertFalse(self.output.exists())

    def test_rejects_changed_evidence_before_output_creation(self):
        original = bundle._members
        def changed(root):
            result = original(root)
            (root / bundle.FILES[0]).write_text('changed after enumeration')
            return result
        with patch.object(bundle, '_members', side_effect=changed):
            with self.assertRaisesRegex(ValueError, 'changed during validation'):
                bundle.create_bundle(self.root, self.output)
        self.assertFalse(self.output.exists())

    def test_rejects_required_file_that_is_a_directory(self):
        path = self.root / bundle.FILES[0]
        path.unlink()
        path.mkdir()
        with self.assertRaises(ValueError):
            bundle.create_bundle(self.root, self.output)
        self.assertFalse(self.output.exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
