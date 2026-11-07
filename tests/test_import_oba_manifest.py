import hashlib
from pathlib import Path
import tempfile
import unittest

from scripts.import_oba_manifest import import_manifest


class Connection:
    def __init__(self, repo):
        self.repo = repo
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def execute(self, query, params=None):
        self.repo.sql.append((query, params))
        if query.startswith('SELECT id FROM collections'):
            rows = [{'id': c['id']} for c in self.repo.collections if c['name'] == params[0]]
            return Cursor(rows[:2])
        if query.startswith('INSERT INTO collections'):
            self.repo.collections.append({'id':params[0], 'name':params[1]})


class Cursor:
    def __init__(self, rows):
        self.rows = rows
    def fetchall(self):
        return self.rows


class Repository:
    def __init__(self, media_root):
        self.media_root = media_root
        self.assets = {}
        self.sql = []
        self.collections = []
        self.memberships = set()
    def connect(self):
        return Connection(self)
    def get_asset(self, asset_id):
        return self.assets.get(asset_id)
    def save_file(self, title, filename, mime, path, size, sha256, asset_id):
        return self.assets.setdefault(asset_id, {'id': asset_id})
    def add_to_collection(self, collection_id, asset_id):
        self.memberships.add((str(collection_id), asset_id))
        return True


class ObaImportTests(unittest.TestCase):
    def test_import_preserves_metadata_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / 'imageset'
            (root / 'images').mkdir(parents=True)
            data = b'\xff\xd8\xff' + b'jpeg'
            (root / 'images/a.jpg').write_bytes(data)
            sha = hashlib.sha256(data).hexdigest()
            item = {'photo_id':'photo-1', 'gallery_id':'demo', 'image_path':'images/a.jpg',
                    'sha256':sha, 'mime_type':'image/jpeg', 'source':{'license_name':'CC BY 4.0'},
                    'identities':[{'name':'member'}], 'metadata':{'origin':'synthetic','captured_at':'2025-01-02'},
                    'raw_exif': {'datetime_original':None}}
            repo = Repository(Path(d) / 'media')
            self.assertEqual(import_manifest(repo, root, [item], 'dataset', emit=lambda _: None)['imported'], 1)
            self.assertEqual(import_manifest(repo, root, [item], 'dataset', emit=lambda _: None)['existing'], 1)
            self.assertEqual(len(repo.assets), 1)
            self.assertEqual(len(repo.collections), 1)
            self.assertEqual(len(repo.memberships), 1)
            metadata_calls = [(sql, params) for sql, params in repo.sql if 'INSERT INTO oba_import_metadata' in sql]
            self.assertEqual(len(metadata_calls), 2)
            self.assertEqual(metadata_calls[0][1][2], 'photo-1')
            self.assertEqual(metadata_calls[0][1][7].obj['origin'], 'synthetic')
            self.assertEqual(metadata_calls[0][1][9].obj, item)
            self.assertFalse(any('INSERT INTO image_metadata' in sql for sql, _ in repo.sql))

    def test_rejects_outside_root_and_bad_hash(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / 'source'; root.mkdir()
            outside = Path(d) / 'outside.jpg'; outside.write_bytes(b'\xff\xd8\xffdata')
            (root / 'link.jpg').symlink_to(outside)
            (root / 'inside.jpg').write_bytes(b'\xff\xd8\xffdata')
            repo = Repository(Path(d) / 'media')
            records = [{'photo_id':str(i), 'image_path':p, 'sha256':'0'*64}
                       for i, p in enumerate(['../outside.jpg','link.jpg','inside.jpg','/tmp/bad.jpg'])]
            result = import_manifest(repo, root, records, 'test', emit=lambda _: None)
            self.assertEqual(result['failed'], 4)
            self.assertEqual(repo.assets, {})

    def test_rejects_malformed_metadata(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / 'source'; root.mkdir()
            content = b'\xff\xd8\xffdata'; (root / 'ok.jpg').write_bytes(content)
            repo = Repository(Path(d) / 'media')
            item = {'photo_id':'a','image_path':'ok.jpg','sha256':hashlib.sha256(content).hexdigest(),
                    'source':['not an object']}
            self.assertEqual(import_manifest(repo, root, [item], 'test', emit=lambda _: None)['failed'], 1)
            self.assertEqual(repo.assets, {})

    def test_selected_manifest_file_field_preserves_photo_and_synthetic_metadata(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / 'selected'; root.mkdir()
            content = b'\xff\xd8\xffdata'; (root / 'web-123.jpg').write_bytes(content)
            repo = Repository(Path(d) / 'media')
            item = {'photo_id':'web-123', 'file':'web-123.jpg',
                    'sha256':hashlib.sha256(content).hexdigest(),
                    'source':{'title':'original title'},
                    'metadata':{'origin':'synthetic','location':'부산'}}
            result = import_manifest(repo, root, [item], 'selected', emit=lambda _: None)
            self.assertEqual(result['imported'], 1)
            metadata_call = next(params for sql, params in repo.sql if 'INSERT INTO oba_import_metadata' in sql)
            self.assertEqual(metadata_call[2], 'web-123')
            self.assertEqual(metadata_call[5].obj['title'], 'original title')
            self.assertEqual(metadata_call[7].obj['origin'], 'synthetic')

    def test_existing_collection_is_reused_and_duplicate_names_fail_before_import(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / 'source'; root.mkdir()
            content = b'\xff\xd8\xffdata'; (root / 'ok.jpg').write_bytes(content)
            item = {'photo_id':'a','file':'ok.jpg','sha256':hashlib.sha256(content).hexdigest()}
            repo = Repository(Path(d) / 'media')
            repo.collections.append({'id':'existing-collection','name':'test'})
            self.assertEqual(import_manifest(repo, root, [item], 'test', emit=lambda _: None)['imported'], 1)
            self.assertEqual(len(repo.collections), 1)
            self.assertEqual(next(iter(repo.memberships))[0], 'existing-collection')
            duplicate = Repository(Path(d) / 'media')
            duplicate.collections = [{'id':'one','name':'test'}, {'id':'two','name':'test'}]
            with self.assertRaisesRegex(ValueError, '여러 개'):
                import_manifest(duplicate, root, [item], 'test', emit=lambda _: None)
            self.assertEqual(duplicate.assets, {})


if __name__ == '__main__':
    unittest.main()
