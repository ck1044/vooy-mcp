"""Exercise real MCP, HTTP upload and PostgreSQL together."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.parse import urlsplit
from urllib.request import urlopen

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


@unittest.skipUnless(os.getenv('VOOY_TEST_DATABASE_URL'), 'temporary PostgreSQL required')
class StorageEndToEnd(unittest.TestCase):
    def test_mcp_image_video_and_text(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            token = 'temporary-integration-test-token-123456789'
            (root / 'token').write_text(token)
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            base = f'http://127.0.0.1:{port}'
            env = os.environ.copy()
            env.update(VOOY_MCP_TOKEN_FILE=str(root / 'token'), VOOY_MCP_EXTRA_TOKEN_DIR=directory,
                       VOOY_MCP_PORT=str(port), VOOY_DATABASE_URL=os.environ['VOOY_TEST_DATABASE_URL'],
                       VOOY_MEDIA_ROOT=str(root / 'media'), VOOY_PUBLIC_BASE_URL=base)
            server = subprocess.Popen([sys.executable, 'server.py'], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            try:
                for _ in range(100):
                    if server.poll() is not None:
                        self.fail('server exited: ' + server.stderr.read().decode())
                    try:
                        urlopen(base + '/healthz', timeout=0.2).close()
                        break
                    except OSError:
                        time.sleep(0.1)
                else:
                    self.fail('server did not start')

                async def run():
                    async with httpx2.AsyncClient(headers={'Authorization': 'Bearer ' + token}, trust_env=False) as client:
                        async with streamable_http_client(base + '/mcp', http_client=client) as (reader, writer):
                            async with ClientSession(reader, writer) as session:
                                await session.initialize()
                                async def call(name, args):
                                    result = await session.call_tool(name, args)
                                    self.assertFalse(result.is_error, str(result))
                                    return result.structured_content or json.loads(result.content[0].text)
                                note = await call('save_text', {'title': '한글 노트', 'text': '저장할 문장'})
                                self.assertEqual(note['text'], '저장할 문장')
                                dl = await call('get_download_link', {'asset_id': note['id']})
                                self.assertEqual((await client.get(dl['url'])).text, '저장할 문장')
                                for kind, filename, data in [('image', 'image.png', b'\x89PNG\r\n\x1a\n' + b'x' * (8 * 1024 * 1024 + 10)),
                                                             ('video', 'video.mp4', b'\0\0\0\x18ftypisom' + b'x' * 1024)]:
                                    upload = await call('create_upload', {'title': filename, 'kind': kind})
                                    uid = upload['upload_id']
                                    token_part = urlsplit(upload['upload_url']).fragment.split('=', 1)[1]
                                    prefix = base + '/api/v1/uploads/' + uid
                                    self.assertEqual((await client.post(prefix + '/exchange', json={'token': token_part})).status_code, 204)
                                    self.assertEqual((await client.put(prefix + '/file', json={'filename': filename, 'size': len(data)})).status_code, 200)
                                    for index in range((len(data) + 8388607) // 8388608):
                                        chunk = data[index * 8388608:(index + 1) * 8388608]
                                        response = await client.put(prefix + f'/parts/{index}', content=chunk, headers={'X-Part-SHA256': hashlib.sha256(chunk).hexdigest()})
                                        self.assertEqual(response.status_code, 200, response.text)
                                    self.assertEqual((await client.post(prefix + '/complete')).status_code, 202)
                                    for _ in range(100):
                                        status = (await client.get(prefix)).json()
                                        if status['state'] in ('completed', 'failed'):
                                            break
                                        await asyncio.sleep(0.1)
                                    self.assertEqual(status['state'], 'completed', status)
                                    saved = await call('get_asset', {'asset_id': status['asset']['id']})
                                    self.assertEqual(saved['kind'], kind)
                                    link = await call('get_download_link', {'asset_id': saved['id']})
                                    self.assertEqual((await client.get(link['url'])).content, data)
                                listed = await call('list_assets', {'query': '한글 노트'})
                                self.assertTrue(any(item['id'] == note['id'] for item in listed['assets']))
                asyncio.run(run())
            finally:
                server.terminate()
                server.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()
