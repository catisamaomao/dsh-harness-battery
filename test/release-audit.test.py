#!/usr/bin/env python3
import importlib.util
import io
import hashlib
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('release_audit', ROOT/'scripts/audit-release.py')
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


class ReleaseAuditTests(unittest.TestCase):
    def test_plain_sources_pass(self):
        result = audit.audit_entries({'index.js': b'export const name="demo"'}, check_bundles=False)
        self.assertTrue(result['ok'])

    def test_private_files_fail(self):
        for name in ('reports/run.json', '.env', '.npmrc', 'data/memorix.sqlite3', 'runtime/frontend/data/profile.report.json'):
            with self.subTest(name=name):
                result = audit.audit_entries({name: b'{}'}, check_bundles=False)
                self.assertFalse(result['ok'])
                self.assertEqual(result['findings'][0]['path'], name)

    def test_matches_never_expose_values(self):
        private = 'sk-' + 'q' * 32
        data = ('value=' + private + '\npath=/Users/' + 'realperson' + '/private\n').encode()
        result = audit.audit_entries({'config.js': data}, check_bundles=False)
        self.assertEqual({x['category'] for x in result['findings']}, {'credential_token', 'user_absolute_path'})
        self.assertNotIn(private, str(result))
        self.assertNotIn('realperson', str(result))

    def test_example_env_and_generic_path_pass(self):
        result = audit.audit_entries({'.env.example': b'API_KEY=\n', 'README.md': b'/Users/example/path'}, check_bundles=False)
        self.assertTrue(result['ok'])

    def test_missing_frozen_bundles_fail(self):
        result = audit.audit_entries({})
        self.assertEqual(len(result['findings']), 2)
        self.assertTrue(all(x['category']=='frozen_bundle_integrity' for x in result['findings']))

    def test_exact_frozen_inventory_and_pin_pass(self):
        files = {'prompt.txt': hashlib.sha256(b'fixed').hexdigest()}
        digest = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        entries = {'bundle/prompt.txt': b'fixed', 'bundle/bundle.lock.json': json.dumps({'files': files, 'bundle_sha256': digest}).encode()}
        with patch.object(audit, 'BUNDLE_PINS', {'bundle': digest}):
            self.assertTrue(audit.audit_entries(entries)['ok'])

    def test_recomputed_lock_cannot_bypass_external_pin(self):
        old_files = {'prompt.txt': hashlib.sha256(b'fixed').hexdigest()}
        pin = hashlib.sha256(json.dumps(old_files, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        changed_files = {'prompt.txt': hashlib.sha256(b'changed').hexdigest()}
        changed_digest = hashlib.sha256(json.dumps(changed_files, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        entries = {'bundle/prompt.txt': b'changed', 'bundle/bundle.lock.json': json.dumps({'files': changed_files, 'bundle_sha256': changed_digest}).encode()}
        with patch.object(audit, 'BUNDLE_PINS', {'bundle': pin}):
            result = audit.audit_entries(entries)
        self.assertFalse(result['ok'])
        self.assertEqual(result['findings'][0]['category'], 'frozen_bundle_integrity')

    def test_duplicate_archive_member_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)/'pkg.tgz'
            with tarfile.open(p, 'w:gz') as tar:
                for content in (b'a', b'b'):
                    entry=tarfile.TarInfo('package/index.js');entry.size=1;tar.addfile(entry,io.BytesIO(content))
            entries, findings = audit.archive_entries(p)
            self.assertEqual(entries['index.js'], b'a')
            self.assertEqual(findings[0]['category'], 'duplicate_archive_member')

    def test_source_symlink_not_followed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            try:
                (root/'link').symlink_to('/not-present-private-path')
            except OSError as error:
                if os.name == 'nt' and getattr(error, 'winerror', None) in (5, 1314):
                    self.skipTest('Windows account cannot create symbolic links')
                raise
            entries, findings = audit.source_entries(root)
            self.assertEqual(entries, {})
            self.assertEqual(findings, [{'path': 'link', 'category': 'symlink'}])

    def test_archive_links_and_traversal_rejected_without_extracting(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)/'pkg.tgz'
            with tarfile.open(p, 'w:gz') as tar:
                good=tarfile.TarInfo('package/index.js');good.size=2;tar.addfile(good, io.BytesIO(b'{}'))
                traversal=tarfile.TarInfo('package/../outside');traversal.size=1;tar.addfile(traversal,io.BytesIO(b'x'))
                link=tarfile.TarInfo('package/link');link.type=tarfile.SYMTYPE;link.linkname='/private';tar.addfile(link)
            entries, findings = audit.archive_entries(p)
            self.assertEqual(entries, {'index.js': b'{}'})
            self.assertEqual({x['category'] for x in findings}, {'unsafe_archive_path','archive_link_or_special_file'})
            self.assertFalse((Path(tmp)/'outside').exists())


if __name__ == '__main__':
    unittest.main()
