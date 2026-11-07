import asyncio
import base64
import hashlib
import io
from pathlib import Path
import tempfile
import unittest

from PIL import Image
from mcp.server import MCPServer
from mcp_types import CallToolResult

from vooy_storage.video_response import video_search_result


class VideoResponseTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        frame_path = root / 'derived' / 'frame.jpg'
        frame_path.parent.mkdir(parents=True)
        out = io.BytesIO()
        Image.new('RGB', (16, 16), 'blue').save(out, format='JPEG')
        self.frame = out.getvalue()
        frame_path.write_bytes(self.frame)
        class Repository:
            media_root = root
        class Rag:
            repository = Repository()
            artifact = {'kind': 'frame', 'asset_id': 'video', 'asset_version_id': 'version',
                        'mime_type': 'image/jpeg', 'object_key': 'derived/frame.jpg',
                        'sha256': hashlib.sha256(self.frame).hexdigest()}
            def get_artifact(self, artifact_id):
                return self.artifact if artifact_id == 'f1' else None
        self.rag = Rag()
        self.row = {'kind': 'video', 'asset_id': 'video', 'version_id': 'version',
                    'download': {'url': 'https://example.test/download'},
                    'locator': {'start_ms': 0, 'end_ms': 10000,
                                'frame_time_ms': 4999, 'artifact_id': 'f1'}}

    def test_frame_block_and_timestamp_are_linked_to_result(self):
        result = video_search_result({'results': [self.row]}, self.rag)
        row = result.structured_content['results'][0]
        self.assertEqual(row['frame_content_index'], 1)
        self.assertEqual(row['locator']['frame_time_ms'], 4999)
        self.assertEqual(base64.b64decode(result.content[1].data), self.frame)
        self.assertNotIn(str(self.rag.repository.media_root), result.model_dump_json())

    def test_unrelated_or_corrupt_artifact_is_never_exposed(self):
        self.rag.artifact['asset_id'] = 'another'
        result = video_search_result({'results': [self.row]}, self.rag)
        self.assertEqual(len(result.content), 1)
        self.rag.artifact['asset_id'] = 'video'
        self.rag.artifact['sha256'] = 'bad'
        result = video_search_result({'results': [self.row]}, self.rag)
        self.assertEqual(len(result.content), 1)

    def test_path_escape_is_rejected(self):
        self.rag.artifact['object_key'] = '../outside'
        result = video_search_result({'results': [self.row]}, self.rag)
        self.assertEqual(len(result.content), 1)

    def test_mcp_tool_transmits_image_content(self):
        server = MCPServer('video-test')
        @server.tool()
        async def search_videos() -> CallToolResult:
            return video_search_result({'results': [self.row]}, self.rag)
        async def run():
            result = await server.call_tool('search_videos', {})
            self.assertFalse(result.is_error)
            index = result.structured_content['results'][0]['frame_content_index']
            self.assertEqual(base64.b64decode(result.content[index].data), self.frame)
        asyncio.run(run())


if __name__ == '__main__':
    unittest.main()
