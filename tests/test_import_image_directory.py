import hashlib
from pathlib import Path
import tempfile
import unittest

from scripts.import_image_directory import import_directory


class FakeRepository:
    def __init__(self, media_root):
        self.media_root = Path(media_root)
        self.assets = {}

    def get_asset(self, asset_id):
        return self.assets.get(str(asset_id))

    def save_file(self, title, filename, mime, path, size, sha, asset_id):
        data = path.read_bytes()
        assert len(data) == size and hashlib.sha256(data).hexdigest() == sha
        return self.assets.setdefault(asset_id, {'id': asset_id, 'source': None})

    def update_asset(self, asset_id, source=None):
        self.assets[asset_id]['source'] = source
        return self.assets[asset_id]


class ImageImportTests(unittest.TestCase):
    def test_import_is_repeatable_and_keeps_sources_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'photos'
            (root / '여행').mkdir(parents=True)
            first = root / '여행' / '바다.PNG'
            second = root / '복사.png'
            content = b'\x89PNG\r\n\x1a\nimage'
            first.write_bytes(content)
            second.write_bytes(content)
            (root / 'note.txt').write_text('ignored')
            repository = FakeRepository(Path(tmp) / 'media')
            lines = []
            result = import_directory(repository, root, 'trip', lines.append)
            self.assertEqual(result, {'imported': 2, 'existing': 0, 'unsupported': 1, 'failed': 0})
            self.assertEqual(len(repository.assets), 2)
            self.assertEqual(first.read_bytes(), content)
            self.assertTrue(any('folder:trip/여행/바다.PNG' == asset['source'] for asset in repository.assets.values()))
            again = import_directory(repository, root, 'trip', lines.append)
            self.assertEqual(again['existing'], 2)
            self.assertEqual(again['imported'], 0)
            first.write_bytes(content + b'changed')
            changed = import_directory(repository, root, 'trip', lines.append)
            self.assertEqual((changed['imported'], changed['existing']), (1, 1))
            self.assertEqual(len(repository.assets), 3)

    def test_invalid_image_is_reported_and_symlink_is_not_followed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'photos'
            root.mkdir()
            (root / 'bad.jpg').write_bytes(b'not a jpeg')
            (root / 'link.jpg').symlink_to(root / 'bad.jpg')
            result = import_directory(FakeRepository(Path(tmp) / 'media'), root, emit=lambda _: None)
            self.assertEqual(result['failed'], 1)
            self.assertEqual(result['unsupported'], 1)

    def test_rejects_source_containing_media_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(ValueError):
                import_directory(FakeRepository(root / 'media'), root, emit=lambda _: None)


if __name__ == '__main__':
    unittest.main()
