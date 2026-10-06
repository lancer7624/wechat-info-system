"""Offline package selection; fake bytes are never executed."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from isolated_interaction import helper_path, verify_package


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.base = self.root / 'window-link'

    def package(self, root, content=b'fake executable, never run'):
        root.mkdir(parents=True)
        executable = root / 'WeChatWindowLink.exe'
        executable.write_bytes(content)
        (root / 'build-manifest.json').write_text(json.dumps({'files': {
            executable.name: hashlib.sha256(content).hexdigest()}}), encoding='utf-8')
        return executable

    def select(self, release):
        package = self.base / 'releases' / release
        (self.base / 'current.json').write_text(json.dumps({
            'release': release,
            'manifest_sha256': hashlib.sha256((package / 'build-manifest.json').read_bytes()).hexdigest(),
        }), encoding='utf-8')
        return package

    def test_legacy_package_remains_readable(self):
        executable = self.package(self.base)
        self.assertEqual(helper_path(self.root), executable)

    def test_next_launch_selects_new_package_without_changing_old_files(self):
        old = self.package(self.base, b'old fake binary')
        new = self.package(self.base / 'releases' / ('a' * 32))
        self.select('a' * 32)
        self.assertEqual(helper_path(self.root), new)
        self.assertEqual(old.read_bytes(), b'old fake binary')

    def test_bad_new_release_never_silently_falls_back_to_old(self):
        self.package(self.base)
        new = self.package(self.base / 'releases' / ('b' * 32))
        self.select('b' * 32)
        new.write_bytes(b'changed')
        with self.assertRaises(ValueError): helper_path(self.root)

    def test_manifest_cannot_change_after_release_selection(self):
        new = self.package(self.base / 'releases' / ('c' * 32))
        self.select('c' * 32)
        (new.parent / 'build-manifest.json').write_text('{"files":{}}', encoding='utf-8')
        with self.assertRaises(ValueError): helper_path(self.root)

    def test_release_pointer_cannot_leave_package_root(self):
        self.package(self.base)
        for release in ('../private', '/outside', 'C:\\outside', '', None):
            (self.base / 'current.json').write_text(json.dumps({'release': release}), encoding='utf-8')
            with self.assertRaises(ValueError): helper_path(self.root)

    def test_manifest_cannot_reference_external_files_or_omit_executable(self):
        self.package(self.base)
        for files in ({}, {'WeChatWindowLink.exe': 'invalid'}, {'WeChatWindowLink.exe': '0' * 64,
                      '../outside': '0' * 64}, {'../outside': '0' * 64, 'WeChatWindowLink.exe': '0' * 64}):
            (self.base / 'build-manifest.json').write_text(json.dumps({'files': files}), encoding='utf-8')
            with self.assertRaises(ValueError): verify_package(self.base)


if __name__ == '__main__': unittest.main()
