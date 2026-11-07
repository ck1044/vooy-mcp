import hashlib
from pathlib import Path
import uuid

from test_repository import RepositoryTests


class CollectionVersionTests(RepositoryTests):
    def test_collection_membership_and_filter(self):
        a = self.repo.save_text('공통', '첫 번째')
        b = self.repo.save_text('공통', '두 번째')
        name = '모음-' + uuid.uuid4().hex
        collection = self.repo.create_collection(name, '기록')
        self.assertTrue(self.repo.add_to_collection(collection['id'], a['id']))
        self.assertTrue(self.repo.add_to_collection(collection['id'], a['id']))
        result = self.repo.list_assets(collection_id=collection['id'])
        self.assertEqual([x['id'] for x in result['assets']], [a['id']])
        self.assertIn(collection['id'], [x['id'] for x in self.repo.list_collections()])
        self.assertTrue(self.repo.remove_from_collection(collection['id'], a['id']))
        self.assertEqual(self.repo.get_asset(a['id'])['text'], '첫 번째')
        self.assertEqual(self.repo.get_asset(b['id'])['text'], '두 번째')

    def test_replacement_retains_original_and_detects_conflicts(self):
        before = b'initial file'
        after = b'replacement file'
        path = Path(self.directory.name) / 'replacement'
        path.write_bytes(before)
        item = self.repo.save_file('자료', 'one.jpg', 'image/jpeg', path, len(before), hashlib.sha256(before).hexdigest())
        old_id = item['version_id']
        path.write_bytes(after)
        request_id = str(uuid.uuid4())
        new = self.repo.replace_file(item['id'], old_id, 'two.jpg', 'image/jpeg', path, len(after), hashlib.sha256(after).hexdigest(), request_id)
        self.assertNotEqual(old_id, new['version_id'])
        self.assertEqual(self.repo.asset_path(self.repo.get_asset(item['id'], old_id)).read_bytes(), before)
        self.assertEqual(self.repo.asset_path(self.repo.get_asset(item['id'])).read_bytes(), after)
        self.assertEqual(new['version_id'], self.repo.replace_file(item['id'], old_id, 'two.jpg', 'image/jpeg', path, len(after), hashlib.sha256(after).hexdigest(), request_id)['version_id'])
        with self.assertRaises(ValueError):
            self.repo.replace_file(item['id'], old_id, 'two.jpg', 'image/jpeg', path, len(after), hashlib.sha256(after).hexdigest(), str(uuid.uuid4()))
        self.assertEqual(len(self.repo.list_versions(item['id'])), 2)
