import hashlib
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

from starlette.applications import Starlette
import httpx2
from vooy_storage.uploads import UploadManager


class FakeRepository:
    def __init__(self):
        self.assets = {}
    def save_file(self, title, filename, mime, path, size, sha, asset_id=None):
        content = Path(path).read_bytes()
        assert len(content) == size and hashlib.sha256(content).hexdigest() == sha
        self.assets[asset_id] = content
        return {'id': asset_id, 'title': title, 'mime_type': mime, 'sha256': sha}
    def get_asset(self, asset_id):
        return {'id': asset_id, 'version_id': 'old-version'}
    def replace_file(self, asset_id, expected_version_id, filename, mime, path, size, sha, request_id):
        self.assets[asset_id] = Path(path).read_bytes()
        return {'id': asset_id, 'version_id': 'new-version'}


class UploadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = FakeRepository()
        self.manager = UploadManager(self.repo, self.temp.name, 'http://testserver')
        self.client = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=Starlette(routes=self.manager.routes())), base_url="http://testserver")
        self.addAsyncCleanup(self.client.aclose)
        self.created = self.manager.create_upload('한글 사진', 'image')
        self.uid = self.created['upload_id']
        self.api = '/api/v1/uploads/' + self.uid
        token = parse_qs(urlsplit(self.created['upload_url']).fragment)['token'][0]
        self.assertEqual((await self.client.post(self.api + '/exchange', json={'token': token})).status_code, 204)
        self.assertEqual((await self.client.post(self.api + '/exchange', json={'token': token})).status_code, 401)

    async def test_upload_and_idempotent_chunk(self):
        data = b'\x89PNG\r\n\x1a\n' + b'file bytes'
        self.assertEqual((await self.client.put(self.api + '/file', json={'filename': 'pic.png', 'size': len(data)})).status_code, 200)
        headers = {'X-Part-SHA256': hashlib.sha256(data).hexdigest()}
        self.assertEqual((await self.client.put(self.api + '/parts/0', content=data, headers=headers)).status_code, 200)
        self.assertEqual((await self.client.put(self.api + '/parts/0', content=data, headers=headers)).status_code, 200)
        self.assertEqual((await self.client.post(self.api + '/complete')).status_code, 202)
        for _ in range(100):
            state = (await self.client.get(self.api)).json()
            if state['state'] in ('completed', 'failed'): break
            await __import__("asyncio").sleep(.02)
        self.assertEqual(state['state'], 'completed', state)
        self.assertEqual(self.repo.assets[self.uid], data)
        self.assertEqual((await self.client.post(self.api + '/complete')).status_code, 200)

    async def test_replace_upload_preserves_asset_identity(self):
        created = self.manager.create_upload('교체', 'image', 'existing-id', 'old-version')
        uid = created['upload_id']
        token = parse_qs(urlsplit(created['upload_url']).fragment)['token'][0]
        api = '/api/v1/uploads/' + uid
        await self.client.post(api + '/exchange', json={'token': token})
        data = b'\x89PNG\r\n\x1a\nnew'
        await self.client.put(api + '/file', json={'filename': 'new.png', 'size': len(data)})
        await self.client.put(api + '/parts/0', content=data, headers={'X-Part-SHA256': hashlib.sha256(data).hexdigest()})
        await self.client.post(api + '/complete')
        for _ in range(100):
            state = (await self.client.get(api)).json()
            if state['state'] in ('completed', 'failed'): break
            await __import__('asyncio').sleep(.02)
        self.assertEqual(state['state'], 'completed', state)
        self.assertEqual(state['asset']['id'], 'existing-id')
        self.assertEqual(state['asset']['version_id'], 'new-version')

    async def test_resume_and_reissued_link(self):
        data = b'\x89PNG\r\n\x1a\n' + b'data'
        await self.client.put(self.api + '/file', json={'filename':'pic.png', 'size':len(data)})
        headers = {'X-Part-SHA256': hashlib.sha256(data).hexdigest()}
        await self.client.put(self.api + '/parts/0', content=data, headers=headers)
        manager = UploadManager(self.repo, self.temp.name, 'http://testserver')
        self.assertEqual(manager.get_status(self.uid)['parts'][0]['sha256'], headers['X-Part-SHA256'])
        new = manager.issue_link(self.uid)
        token = parse_qs(urlsplit(new['upload_url']).fragment)['token'][0]
        other = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=Starlette(routes=manager.routes())), base_url="http://testserver")
        self.addAsyncCleanup(other.aclose)
        self.assertEqual((await other.get(self.api)).status_code, 401)
        self.assertEqual((await other.post(self.api + '/exchange', json={'token':token})).status_code, 204)
        self.assertEqual((await other.get(self.api)).status_code, 200)
        manager.request_complete(self.uid)

    async def test_large_upload_resumes_across_manager_restart(self):
        size = 8 * 1024 * 1024
        first = b'\x89PNG\r\n\x1a\n' + b'x' * (size - 8)
        second = b'last chunk'
        await self.client.put(self.api + '/file', json={'filename': 'large.png', 'size': len(first) + len(second)})
        response = await self.client.put(self.api + '/parts/0', content=first, headers={'X-Part-SHA256': hashlib.sha256(first).hexdigest()})
        self.assertEqual(response.status_code, 200)
        manager = UploadManager(self.repo, self.temp.name, 'http://testserver')
        self.assertEqual(len(manager.get_status(self.uid)['parts']), 1)
        response = await self.client.put(self.api + '/parts/1', content=second, headers={'X-Part-SHA256': hashlib.sha256(second).hexdigest()})
        self.assertEqual(response.status_code, 200)
        await self.client.post(self.api + '/complete')
        for _ in range(200):
            state = (await self.client.get(self.api)).json()
            if state['state'] in ('completed', 'failed'): break
            await __import__('asyncio').sleep(.02)
        self.assertEqual(state['state'], 'completed', state)
        self.assertEqual(self.repo.assets[self.uid], first + second)

    async def test_rejects_wrong_origin(self):
        response = await self.client.put(self.api + '/file', headers={'Origin': 'https://other.example'}, json={'filename': 'x.txt', 'size': 2})
        self.assertEqual(response.status_code, 403)

    async def test_rejects_bad_hash_and_incomplete_finalize(self):
        await self.client.put(self.api + '/file', json={'filename':'pic.png', 'size':8})
        self.assertEqual((await self.client.put(self.api + '/parts/0', content=b'12345678', headers={'X-Part-SHA256':'x'})).status_code, 422)
        self.assertEqual((await self.client.post(self.api + '/complete')).status_code, 409)


if __name__ == '__main__': unittest.main()
