import asyncio
import base64
import hashlib
import io
import tempfile
import unittest
from pathlib import Path

from mcp.server import MCPServer
from mcp_types import CallToolResult
from PIL import Image

from vooy_storage.image_response import photo_result, search_result


class Repository:
    def __init__(self, root, data):
        self.media_root = Path(root)
        self.path = self.media_root / 'originals' / 'image.jpg'
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(data)
        self.asset = {'id': 'a', 'version_id': 'v', 'kind': 'image', 'mime_type': 'image/jpeg',
                      'filename': 'image.jpg', 'title': '여행', 'size_bytes': len(data),
                      'sha256': hashlib.sha256(data).hexdigest(), 'object_key': 'originals/image.jpg'}

    def get_asset(self, asset_id, version_id=None):
        if asset_id != 'a' or version_id not in (None, 'v'):
            return None
        return self.asset

    def asset_path(self, asset):
        return self.path


class Downloads:
    def link(self, asset_id, version_id):
        return {'url': 'https://example.com/download?token=redacted', 'expires_at': 2000000000}
    def image_link(self, asset_id, version_id):
        return {'url': 'https://example.com/image?token=redacted', 'expires_at': 2000000000}


class Rag:
    def __init__(self, repository, data):
        self.repository = repository
        path = repository.media_root / 'derived' / 'thumb.jpg'
        path.parent.mkdir()
        path.write_bytes(data)
        self.artifact = {'id': 't', 'asset_id': 'a', 'asset_version_id': 'v', 'kind': 'thumbnail',
                         'mime_type': 'image/jpeg', 'object_key': 'derived/thumb.jpg',
                         'sha256': hashlib.sha256(data).hexdigest()}

    def list_artifacts(self, asset_id):
        return [self.artifact] if asset_id == 'a' else []

    def get_artifact(self, artifact_id):
        return self.artifact if artifact_id == 't' else None


class ImageResponses(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        img = io.BytesIO()
        Image.new('RGB', (16, 16), 'red').save(img, format='JPEG')
        self.data = img.getvalue()
        self.repo = Repository(self.directory.name, self.data)
        self.rag = Rag(self.repo, self.data)
        self.downloads = Downloads()

    def test_search_inline_index_and_selected_original_via_mcp(self):
        server = MCPServer('image-test')
        payload = {'results': [{'asset_id': 'a', 'version_id': 'v', 'kind': 'image'}]}

        @server.tool()
        async def search_photos() -> CallToolResult:
            return search_result(payload, self.rag)

        @server.tool()
        async def get_photo(asset_id: str) -> CallToolResult:
            return photo_result(asset_id, self.repo, self.downloads)

        async def run():
            result = await server.call_tool('search_photos', {})
            self.assertFalse(result.is_error)
            index = result.structured_content['results'][0]['preview_content_index']
            self.assertEqual(index, 1)
            self.assertEqual(base64.b64decode(result.content[index].data), self.data)
            selected = await server.call_tool('get_photo', {'asset_id': 'a'})
            self.assertFalse(selected.is_error)
            self.assertEqual(base64.b64decode(selected.content[1].data), self.data)
            self.assertEqual(selected.content[2].type, 'resource_link')
            self.assertEqual(selected.structured_content['image_url'], 'https://example.com/image?token=redacted')
            self.assertEqual(selected.structured_content['download_url'], 'https://example.com/download?token=redacted')
            self.assertEqual(selected.structured_content['delivery_status'], 'not_sent')
            self.assertNotIn(self.directory.name, selected.model_dump_json())
        asyncio.run(run())

    def test_missing_or_unsafe_thumbnail_does_not_expose_bytes(self):
        self.rag.artifact['object_key'] = '../outside.jpg'
        result = search_result({'results': [{'asset_id': 'a', 'version_id': 'v', 'kind': 'image'}]}, self.rag)
        self.assertEqual(len(result.content), 1)
        self.assertNotIn('preview_content_index', result.structured_content['results'][0])

    def test_corrupted_original_is_rejected(self):
        self.repo.path.write_bytes(self.data + b'changed')
        with self.assertRaises(ValueError):
            photo_result('a', self.repo, self.downloads)

    def test_visual_description_is_bound_to_the_returned_image(self):
        class Captioner:
            def describe_many(self, images):
                self.images = images
                return [{'photo_id': x['photo_id'], 'version_id': x['version_id'],
                         'status': 'available', 'description': '붉은 이미지', 'verified': False} for x in images]
        captioner = Captioner()
        result = search_result({'results': [{'asset_id': 'a', 'version_id': 'v', 'kind': 'image'}]},
                               self.rag, captioner=captioner)
        self.assertEqual(captioner.images[0]['image_bytes'], self.data)
        self.assertEqual(result.structured_content['results'][0]['visual_observation']['description'], '붉은 이미지')
        self.assertIn('붉은 이미지', result.content[0].text)

    def test_large_original_returns_download_without_inline_bytes(self):
        self.repo.path.write_bytes(b'X' * (8 * 1024 * 1024 + 1))
        result = photo_result('a', self.repo, self.downloads)
        self.assertFalse(result.structured_content['image_content_included'])
        self.assertEqual([c.type for c in result.content], ['text', 'resource_link'])

    def test_oba_provenance_is_version_bound_and_whitelisted(self):
        class Cursor:
            def __init__(self, value): self.value = value
            def fetchone(self): return self.value
        class Connection:
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def execute(self, sql, params=None):
                if 'to_regclass' in sql:
                    return Cursor({'name': 'oba_import_metadata'})
                if params != ('a', 'v'):
                    return Cursor(None)
                return Cursor({'photo_id': 'photo-1',
                    'source': {'author': 'Author', 'attribution': 'Credit',
                               'page_url': 'https://example.org/photo?token=secret',
                               'description': 'private data', 'article_identities': ['Member']},
                    'identities': [], 'metadata': {'origin': 'synthetic', 'location': '제주',
                                                    'latitude': 55, 'private': 'secret'}})
        self.repo.connect = lambda: Connection()
        photo = photo_result('a', self.repo, self.downloads)
        public = photo.structured_content
        self.assertEqual(public['photo_id'], 'a')
        self.assertEqual(public['source_photo_id'], 'photo-1')
        self.assertEqual(public['identities'], ['Member'])
        self.assertEqual(public['metadata'], {'origin': 'synthetic', 'location': '제주'})
        self.assertEqual(public['source']['page_url'], 'https://example.org/photo')
        self.assertNotIn('private data', photo.model_dump_json())
        self.assertNotIn('secret', photo.model_dump_json())
        found = search_result({'results': [{'asset_id': 'a', 'version_id': 'v', 'kind': 'image'}]}, self.rag)
        self.assertEqual(found.structured_content['results'][0]['metadata']['origin'], 'synthetic')
        other_version = search_result({'results': [{'asset_id': 'a', 'version_id': 'other', 'kind': 'image'}]}, self.rag)
        self.assertNotIn('source_photo_id', other_version.structured_content['results'][0])

    def test_missing_oba_table_does_not_break_existing_asset(self):
        class Cursor:
            def fetchone(self): return {'name': None}
        class Connection:
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def execute(self, *_): return Cursor()
        self.repo.connect = lambda: Connection()
        self.assertNotIn('source_photo_id', photo_result('a', self.repo, self.downloads).structured_content)


if __name__ == '__main__':
    unittest.main()
