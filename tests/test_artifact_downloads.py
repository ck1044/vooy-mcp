import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from vooy_storage.artifact_downloads import ArtifactDownloads
from vooy_storage.downloads import DownloadManager


class ArtifactTests(unittest.TestCase):
    def test_preview_is_inline_short_lived_and_requires_current_thumbnail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'derived').mkdir()
            (root / 'derived' / 'thumb.jpg').write_bytes(b'jpg')
            class Rag:
                repository = SimpleNamespace(media_root=root)
                artifact = {'kind': 'thumbnail', 'object_key': 'derived/thumb.jpg',
                            'filename': 'thumb.jpg', 'mime_type': 'image/jpeg', 'asset_version_id': 'version-1'}
                def get_artifact(self, _artifact_id):
                    return self.artifact
            rag = Rag()
            now = [1000]
            manager = ArtifactDownloads(rag, DownloadManager(None, 'https://example.org', 'secret', clock=lambda: now[0]))
            self.assertIsNone(manager.preview_link('artifact-1', 'older-version'))
            link = manager.preview_link('artifact-1', 'version-1')
            token = link['url'].split('token=')[1]
            request = SimpleNamespace(path_params={'artifact_id': 'artifact-1'}, query_params={'token': token})
            response = asyncio.run(manager.preview(request))
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.headers['content-disposition'].startswith('inline'))
            rag.artifact = None
            self.assertEqual(asyncio.run(manager.preview(request)).status_code, 404)
            now[0] = 1900
            self.assertEqual(asyncio.run(manager.preview(request)).status_code, 403)

    def test_link_requires_active_artifact_and_confines_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'derived').mkdir()
            (root / 'derived' / 'sample.txt').write_text('sample')
            class Rag:
                repository = SimpleNamespace(media_root=root)
                artifact = {'object_key': 'derived/sample.txt', 'filename': 'sample.txt', 'mime_type': 'text/plain'}
                def get_artifact(self, artifact_id):
                    return self.artifact
            rag = Rag()
            downloads = DownloadManager(None, 'https://example.org', 'secret', clock=lambda: 1000)
            manager = ArtifactDownloads(rag, downloads)
            link = manager.link('artifact-1')
            token = link['url'].split('token=')[1]
            request = SimpleNamespace(path_params={'artifact_id':'artifact-1'}, query_params={'token':token})
            self.assertEqual(asyncio.run(manager.download(request)).status_code, 200)
            rag.artifact['object_key'] = '../outside'
            self.assertEqual(asyncio.run(manager.download(request)).status_code, 404)
            rag.artifact = None
            self.assertEqual(asyncio.run(manager.download(request)).status_code, 404)


if __name__ == '__main__':
    unittest.main()
