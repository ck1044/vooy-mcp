import tempfile
import unittest
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.backup_storage import copy_media, validate_backup, validate_references
from scripts.daily_backup import required_capacity
from scripts import daily_backup


class BackupManifestTests(unittest.TestCase):
    def test_copy_and_verify_detects_corruption(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            source, dest = root / 'media', root / 'backup'
            (source / 'originals' / 'asset').mkdir(parents=True)
            (source / 'originals' / 'asset' / 'source').write_bytes(b'valid contents')
            (source / 'derived').mkdir()
            (source / 'derived' / 'frame').write_bytes(b'frame')
            dest.mkdir()
            manifest = copy_media(source, dest)
            self.assertEqual(len(manifest), 2)
            validate_backup(dest, manifest)
            (dest / 'media' / 'derived' / 'frame').write_bytes(b'corrupt')
            with self.assertRaises(ValueError):
                validate_backup(dest, manifest)

    def test_rejects_symlinks_and_path_traversal(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            source, dest = root / 'media', root / 'backup'
            (source / 'originals').mkdir(parents=True)
            dest.mkdir()
            (source / 'originals' / 'escape').symlink_to(root)
            with self.assertRaises(ValueError):
                copy_media(source, dest)
            with self.assertRaises(ValueError):
                validate_backup(dest, [{'path': '../escape', 'size': 0, 'sha256': '0' * 64}])

    def test_database_references_match_backed_up_files(self):
        item = {'path': 'originals/one/source', 'size': 10, 'sha256': 'a' * 64}
        validate_references([item], [item])
        with self.assertRaises(ValueError):
            validate_references([item], [{'path': item['path'], 'size': 10, 'sha256': 'b' * 64}])
        with self.assertRaises(ValueError):
            validate_references([], [item])

    def test_capacity_includes_headroom(self):
        self.assertEqual(required_capacity(100, 50), 2 * 1024 ** 3 + 165)

    def test_insufficient_space_preserves_existing_backup_and_reports_failure(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            media, backups = root / 'media', root / 'backups'
            media.mkdir()
            backups.mkdir()
            (backups / 'previous').write_text('safe')
            def fake_run(command, **kwargs):
                if command[0] == 'docker':
                    return SimpleNamespace(stdout='100\n')
                if command[0] == 'logger':
                    return SimpleNamespace(returncode=0)
                self.fail('백업 명령이 공간 부족에도 실행됐습니다')
            argv = ['daily_backup.py', '--media-root', str(media), '--destination', str(backups)]
            with patch.object(daily_backup, 'PROJECT_ROOT', root), patch('sys.argv', argv), \
                 patch.object(daily_backup.subprocess, 'run', side_effect=fake_run), \
                 patch.object(daily_backup.shutil, 'disk_usage', return_value=SimpleNamespace(free=1)):
                with self.assertRaises(SystemExit):
                    daily_backup.main()
            self.assertEqual((backups / 'previous').read_text(), 'safe')
            status = json.loads((root / '.secrets' / 'backup-status.json').read_text())
            self.assertEqual(status['state'], 'failed')
            self.assertIn('공간 부족', status['error'])


if __name__ == '__main__':
    unittest.main()
