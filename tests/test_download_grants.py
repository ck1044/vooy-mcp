import tempfile
import unittest
import asyncio
import io
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx2
from PIL import Image
from starlette.applications import Starlette

from vooy_storage.downloads import DownloadManager


class Repository:
    def __init__(self, root):
        self.path = Path(root) / "file.txt"
        self.path.write_text("안녕하세요", encoding="utf-8")
        self.asset = {"id": "asset-1", "filename": "file.txt", "mime_type": "text/plain"}

    def get_asset(self, asset_id):
        return self.asset if self.asset and self.asset["id"] == asset_id else None

    def asset_path(self, asset):
        return self.path


class DownloadGrantTests(unittest.TestCase):
    def test_grant_binds_asset_and_expiry(self):
        with tempfile.TemporaryDirectory() as root:
            manager = DownloadManager(Repository(root), "https://example.org", "strong-test-secret", clock=lambda: 1000)
            token = manager.issue("asset-1")
            self.assertTrue(manager.valid("asset-1", token))
            self.assertFalse(manager.valid("asset-2", token))
            manager.clock = lambda: 1901
            self.assertFalse(manager.valid("asset-1", token))
            self.assertFalse(manager.valid("asset-1", token + "x"))

    def test_link_requires_existing_asset(self):
        with tempfile.TemporaryDirectory() as root:
            manager = DownloadManager(Repository(root), "https://example.org", "strong-test-secret", clock=lambda: 1000)
            self.assertIsNone(manager.link("missing"))
            link = manager.link("asset-1")
            self.assertIn("https://example.org/api/v1/downloads/asset-1?token=", link["url"])
            self.assertEqual(link["expires_at"], 1900)

    def test_version_is_part_of_signature(self):
        with tempfile.TemporaryDirectory() as root:
            manager = DownloadManager(Repository(root), "https://example.org", "strong-test-secret", clock=lambda: 1000)
            token = manager.issue("asset-1", "version-1")
            self.assertTrue(manager.valid("asset-1", token, "version-1"))
            self.assertFalse(manager.valid("asset-1", token, "version-2"))

    def test_artifact_grant_is_separate_from_asset_grant(self):
        with tempfile.TemporaryDirectory() as root:
            manager = DownloadManager(Repository(root), 'https://example.org', 'strong-test-secret', clock=lambda: 1000)
            token = manager.issue('artifact:abc')
            self.assertTrue(manager.valid('artifact:abc', token))
            self.assertFalse(manager.valid('abc', token))

    def test_text_download_and_deleted_asset(self):
        with tempfile.TemporaryDirectory() as root:
            repo = Repository(root)
            repo.asset.update({"kind": "text", "text": "한글 원문", "filename": None})
            manager = DownloadManager(repo, "https://example.org", "strong-test-secret", clock=lambda: 1000)
            token = manager.issue("asset-1")
            request = SimpleNamespace(path_params={"asset_id": "asset-1"}, query_params={"token": token})
            response = asyncio.run(manager.download(request))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.body.decode(), "한글 원문")
            repo.asset = None
            self.assertEqual(asyncio.run(manager.download(request)).status_code, 404)

    def test_inline_image_http_head_and_signed_namespace(self):
        with tempfile.TemporaryDirectory() as root:
            repo = Repository(root)
            output = io.BytesIO()
            Image.new('RGB', (10, 10), 'red').save(output, format='JPEG')
            data = output.getvalue()
            repo.path.write_bytes(data)
            repo.asset.update({'kind': 'image', 'version_id': 'version-1', 'mime_type': 'image/jpeg', 'filename': 'x.jpg'})
            def get_asset(asset_id, version_id=None):
                return repo.asset if repo.asset and asset_id == 'asset-1' and version_id in (None, 'version-1') else None
            repo.get_asset = get_asset
            manager = DownloadManager(repo, 'https://example.org', 'strong-test-secret', clock=lambda: 1000)
            inline = manager.image_link('asset-1', 'version-1')
            self.assertIn('/api/v1/images/asset-1?', inline['url'])
            app = Starlette(routes=manager.routes())
            async def run():
                async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app)) as client:
                    result = await client.get(inline['url'])
                    self.assertEqual(result.status_code, 200)
                    self.assertEqual(result.content, data)
                    self.assertEqual(result.headers['content-type'], 'image/jpeg')
                    self.assertTrue(result.headers['content-disposition'].startswith('inline;'))
                    head = await client.head(inline['url'])
                    self.assertEqual(head.status_code, 200)
                    self.assertEqual(head.content, b'')
                    self.assertEqual(int(head.headers['content-length']), len(data))
                    self.assertEqual((await client.get(inline['url'].replace('version-1', 'version-2'))).status_code, 403)
                    download = manager.link('asset-1', 'version-1')
                    token = parse_qs(urlsplit(download['url']).query)['token'][0]
                    tampered = inline['url'].split('?')[0] + '?version_id=version-1&token=' + token
                    self.assertEqual((await client.get(tampered)).status_code, 403)
                    manager.clock = lambda: 1900
                    self.assertEqual((await client.get(inline['url'])).status_code, 403)
            asyncio.run(run())

    def test_inline_rejects_non_image_and_deleted_asset(self):
        with tempfile.TemporaryDirectory() as root:
            repo = Repository(root)
            manager = DownloadManager(repo, 'https://example.org', 'strong-test-secret', clock=lambda: 1000)
            self.assertIsNone(manager.image_link('asset-1'))
            repo.asset = None
            self.assertIsNone(manager.image_link('asset-1'))

    def test_video_inline_supports_range_and_version_bound_signature(self):
        with tempfile.TemporaryDirectory() as root:
            repo = Repository(root)
            repo.path.write_bytes(b'0123456789')
            repo.asset.update({'kind': 'video', 'version_id': 'version-1',
                               'mime_type': 'video/mp4', 'filename': 'x.mp4'})
            repo.get_asset = lambda asset_id, version_id=None: repo.asset if asset_id == 'asset-1' and version_id in (None, 'version-1') else None
            manager = DownloadManager(repo, 'https://example.org', 'strong-test-secret', clock=lambda: 1000)
            link = manager.media_link('asset-1', 'version-1')
            app = Starlette(routes=manager.routes())
            async def run():
                async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app)) as client:
                    response = await client.get(link['url'], headers={'Range': 'bytes=2-5'})
                    self.assertEqual(response.status_code, 206)
                    self.assertEqual(response.content, b'2345')
                    self.assertTrue(response.headers['content-disposition'].startswith('inline;'))
                    self.assertEqual((await client.get(link['url'].replace('version-1', 'version-2'))).status_code, 403)
            asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
