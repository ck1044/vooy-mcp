"""Group image uploads retain their context and the original bytes."""

import asyncio
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from starlette.applications import Starlette

from vooy_storage.uploads import UploadManager
from vooy_storage.mcp_tools import register_storage_tools


class RecordingRepository:
    def __init__(self):
        self.files = {}

    def save_file(self, title, filename, mime, path, size, sha, asset_id=None,
                  tags=None, source=None):
        data = Path(path).read_bytes()
        self.files[asset_id] = data
        return {'id': asset_id, 'title': title, 'sha256': sha,
                'size_bytes': size, 'tags': tags, 'source': source}


class CapturingServer:
    def __init__(self):
        self.tools = {}

    def tool(self, **_kwargs):
        def register(fn):
            self.tools[fn.__name__] = fn
            return fn
        return register


class GroupUploadTests(unittest.IsolatedAsyncioTestCase):
    async def test_group_context_is_preserved_when_image_upload_finishes(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = RecordingRepository()
            manager = UploadManager(repo, directory, 'http://testserver')
            server = CapturingServer()
            register_storage_tools(server, repo, manager, object())
            created = server.tools['create_upload'](
                '여행 사진', 'image', tags=['단톡방', '여행'],
                source='Vooy 단톡방: 가족')
            # The pending upload must survive a server restart with its context.
            manager = UploadManager(repo, directory, 'http://testserver')
            uid = created['upload_id']
            token = parse_qs(urlsplit(created['upload_url']).fragment)['token'][0]
            api = '/api/v1/uploads/' + uid
            data = b'\x89PNG\r\n\x1a\n' + b'group image test'
            async with httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=Starlette(routes=manager.routes())),
                base_url='http://testserver',
            ) as client:
                self.assertEqual((await client.post(api + '/exchange', json={'token': token})).status_code, 204)
                self.assertEqual((await client.put(api + '/file', json={'filename': 'photo.png', 'size': len(data)})).status_code, 200)
                self.assertEqual((await client.put(api + '/parts/0', content=data,
                    headers={'X-Part-SHA256': hashlib.sha256(data).hexdigest()})).status_code, 200)
                self.assertEqual((await client.post(api + '/complete')).status_code, 202)
                for _ in range(100):
                    state = (await client.get(api)).json()
                    if state['state'] in ('completed', 'failed'):
                        break
                    await asyncio.sleep(.02)
            self.assertEqual(state['state'], 'completed', state)
            self.assertEqual(repo.files[uid], data)
            self.assertEqual(state['asset']['sha256'], hashlib.sha256(data).hexdigest())
            self.assertEqual(state['asset']['tags'], ['단톡방', '여행'])
            self.assertEqual(state['asset']['source'], 'Vooy 단톡방: 가족')

    async def test_invalid_tags_are_rejected_before_an_upload_is_created(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = UploadManager(RecordingRepository(), directory, 'http://testserver')
            with self.assertRaisesRegex(ValueError, '태그'):
                manager.create_upload('사진', 'image', tags=['x' * 101], source='Vooy 단톡방')
            self.assertEqual(list(manager.root.iterdir()), [])


@unittest.skipUnless(os.getenv('VOOY_TEST_DATABASE_URL'), 'temporary PostgreSQL required')
class GroupUploadIntegrationTests(unittest.TestCase):
    def test_mcp_and_postgres_keep_group_context_and_original_image(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            token = 'group-integration-test-token-123456789'
            (root / 'token').write_text(token)
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            base = f'http://127.0.0.1:{port}'
            env = os.environ.copy()
            env.update(VOOY_MCP_TOKEN_FILE=str(root / 'token'), VOOY_MCP_EXTRA_TOKEN_DIR=directory,
                       VOOY_MCP_PORT=str(port), VOOY_DATABASE_URL=os.environ['VOOY_TEST_DATABASE_URL'],
                       VOOY_MEDIA_ROOT=str(root / 'media'), VOOY_PUBLIC_BASE_URL=base)
            server = subprocess.Popen([sys.executable, 'server.py'], env=env,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            try:
                for _ in range(100):
                    if server.poll() is not None:
                        self.fail('server exited: ' + server.stderr.read().decode())
                    try:
                        urlopen(base + '/healthz', timeout=.2).close()
                        break
                    except OSError:
                        time.sleep(.1)
                else:
                    self.fail('server did not start')

                async def run():
                    async with httpx2.AsyncClient(headers={'Authorization': 'Bearer ' + token},
                                                  trust_env=False) as client:
                        async with streamable_http_client(base + '/mcp', http_client=client) as (reader, writer):
                            async with ClientSession(reader, writer) as session:
                                await session.initialize()

                                async def call(name, args):
                                    result = await session.call_tool(name, args)
                                    self.assertFalse(result.is_error, str(result))
                                    return result.structured_content or json.loads(result.content[0].text)

                                upload = await call('create_upload', {
                                    'title': '가족 사진', 'kind': 'image',
                                    'source': 'Vooy 단톡방: 가족', 'tags': ['단톡방', '여행']})
                                uid = upload['upload_id']
                                grant = parse_qs(urlsplit(upload['upload_url']).fragment)['token'][0]
                                api = base + '/api/v1/uploads/' + uid
                                data = b'\x89PNG\r\n\x1a\n' + b'integration image'
                                self.assertEqual((await client.post(api + '/exchange', json={'token': grant})).status_code, 204)
                                self.assertEqual((await client.put(api + '/file', json={'filename':'group.png', 'size':len(data)})).status_code, 200)
                                self.assertEqual((await client.put(api + '/parts/0', content=data,
                                    headers={'X-Part-SHA256':hashlib.sha256(data).hexdigest()})).status_code, 200)
                                self.assertEqual((await client.post(api + '/complete')).status_code, 202)
                                for _ in range(100):
                                    state = (await client.get(api)).json()
                                    if state['state'] in ('completed', 'failed'):
                                        break
                                    await asyncio.sleep(.05)
                                self.assertEqual(state['state'], 'completed', state)
                                asset = await call('get_asset', {'asset_id': uid})
                                self.assertEqual(asset['source'], 'Vooy 단톡방: 가족')
                                self.assertEqual(asset['tags'], ['단톡방', '여행'])
                                self.assertEqual(asset['sha256'], hashlib.sha256(data).hexdigest())
                                self.assertEqual(next((root / 'media' / 'originals' / uid).rglob('source')).read_bytes(), data)
                asyncio.run(run())
            finally:
                server.terminate()
                server.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()
