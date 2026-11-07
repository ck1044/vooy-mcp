import asyncio
import unittest
from unittest.mock import patch

from mcp.server import MCPServer

from vooy_storage.mcp_tools import register_storage_tools


class FakeServer:
    def __init__(self):
        self.tools = {}

    def tool(self, **_kwargs):
        def register(fn):
            self.tools[fn.__name__] = fn
            return fn
        return register


class FakeRepository:
    def save_text(self, title, text, tags=None, source=None, request_key=None):
        return {"id": "one", "title": title, "text": text, "object_key": "private-path"}

    def get_asset(self, asset_id):
        return {"id": asset_id, "object_key": "private-path", "title": "제목"}

    def list_assets(self, **kwargs):
        return {"assets": [self.get_asset("one")], "next_cursor": None}


class FakeUploads:
    def create_upload(self, title, kind):
        return {"upload_id": "upload-1", "upload_url": "https://example.org/upload"}

    def get_status(self, upload_id):
        return {"state": "completed", "upload_id": upload_id}


class FakeDownloads:
    def link(self, asset_id):
        return {"url": "https://example.org/download"}


class ToolTests(unittest.TestCase):
    def test_bts_browse_is_narrow_and_respects_gallery_scope(self):
        class Repo(FakeRepository):
            def list_collections(self):
                return [{'id':'bts','name':'bts-daily'},{'id':'personal','name':'personal'}]
        class Rag:
            def list_spaces(self): return [{'id':'space'}]
            def browse_collection_images(self, space_id, collection_id, limit, require_unique_policy=False):
                self.browsed = (space_id,collection_id,limit,require_unique_policy)
                return {'mode':'browse','status':'ok','results':[]}
            def search_vector(self, *args, **kwargs):
                self.search_kwargs = kwargs
                return {'results':[]}
        class Encoder:
            def encode(self, query, selected): return [1,0]
        server, rag = FakeServer(), Rag()
        register_storage_tools(server, Repo(), FakeUploads(), FakeDownloads(), rag=rag, encoder=Encoder())
        with patch('vooy_storage.image_response.search_result', side_effect=lambda payload, rag, **kwargs: payload):
            call=server.tools['search_photos']
            self.assertEqual(call('BTS 사진 보여줘')['mode'],'browse')
            self.assertEqual(rag.browsed, ('space','bts',3,True))
            self.assertEqual(call('방탄소년단 이미지',gallery_id='bts-daily')['mode'],'browse')
            self.assertEqual(rag.browsed, ('space','bts',3,False))
            self.assertNotIn('mode',call('BTS 바다 사진'))
            self.assertNotIn('mode',call('BTS 사진',collection_id='personal'))
            self.assertNotIn('mode',call('BTS 사진',captured_from='2025-01-01'))

    def test_photo_search_labels_uncalibrated_results_as_candidates(self):
        class Rag:
            def list_spaces(self):
                return [{'id': 'space'}]
            def search_vector(self, *args, **kwargs):
                return {'results': [{'asset_id':'a','version_id':'v','kind':'text'}], 'relevance_unverified':True}
        class Encoder:
            def encode(self, query, selected): return [1, 0]
        class Downloads:
            def link(self, asset_id, version_id): return {'url':'https://example.org/file'}
        server = FakeServer()
        register_storage_tools(server, FakeRepository(), FakeUploads(), Downloads(), rag=Rag(), encoder=Encoder())
        result = server.tools['search_photos']('여행')
        self.assertEqual(result.structured_content['status'],'candidates_unverified')
        self.assertIn('후보',result.structured_content['message'])

    def test_uncalibrated_candidates_with_missing_dates_prioritize_date_notice(self):
        class Rag:
            def list_spaces(self): return [{'id':'space'}]
            def search_vector(self, *args, **kwargs):
                return {'results':[{'asset_id':'a','version_id':'v','kind':'text'}],
                        'relevance_unverified':True,'missing_capture_date_count':2}
        class Encoder:
            def encode(self, query, selected): return [1,0]
        class Downloads:
            def link(self, asset_id, version_id): return {'url':'https://example.org/file'}
        server = FakeServer()
        register_storage_tools(server, FakeRepository(), FakeUploads(), Downloads(), rag=Rag(), encoder=Encoder())
        result = server.tools['search_photos']('여행', captured_from='2025-01-01')
        self.assertEqual(result.structured_content['status'],'date_metadata_missing')
        self.assertIn('후보',result.structured_content['message'])

    def test_photo_search_requests_auto_policy_only_without_explicit_collection(self):
        class Rag:
            def list_spaces(self):
                return [{'id': 'space'}]
            def search_vector(self, *args, **kwargs):
                self.params = kwargs
                return {'results': [], 'missing_capture_date_count': 2}
        class Encoder:
            def encode(self, query, selected):
                return [1, 0]
        server, rag = FakeServer(), Rag()
        register_storage_tools(server, FakeRepository(), FakeUploads(), FakeDownloads(), rag=rag, encoder=Encoder())
        result = server.tools['search_photos']('여행', captured_from='2025-01-01')
        self.assertTrue(rag.params['auto_collection_policy'])
        self.assertEqual(result.structured_content['status'], 'date_metadata_missing')
        server.tools['search_photos']('여행', collection_id='chosen')
        self.assertEqual(rag.params['collection_id'], 'chosen')
        self.assertNotIn('auto_collection_policy', rag.params)

    def test_date_filtered_empty_results_explain_missing_capture_metadata(self):
        class Rag:
            def list_spaces(self):
                return [{'id': 'space'}]
            def search_vector(self, *args, **kwargs):
                return {'results': [], 'missing_capture_date_count': 3}
        class Encoder:
            def encode(self, query, selected):
                return [1, 0]
        server = FakeServer()
        register_storage_tools(server, FakeRepository(), FakeUploads(), FakeDownloads(), rag=Rag(), encoder=Encoder())
        response = server.tools['search_assets']('여행', captured_from='2025-01-01')
        self.assertEqual(response['status'], 'date_metadata_missing')
        self.assertIn('촬영 날짜가 없는', response['message'])
        self.assertEqual(response['missing_capture_date_count'], 3)

    def test_ingestion_uses_configured_pipeline_unless_explicitly_overridden(self):
        class Rag:
            def enqueue(self, asset_id, space_id, revision, request_key):
                return {'revision': revision, 'request_key': request_key}
        server = FakeServer()
        register_storage_tools(server, FakeRepository(), FakeUploads(), FakeDownloads(),
                               rag=Rag(), default_pipeline_revision='openrouter-image-v1')
        call = server.tools['request_ingestion']
        self.assertEqual(call('image', 'space')['revision'], 'openrouter-image-v1')
        self.assertEqual(call('image', 'space', 'external-v2', 'key'),
                         {'revision': 'external-v2', 'request_key': 'key'})
        legacy = FakeServer()
        register_storage_tools(legacy, FakeRepository(), FakeUploads(), FakeDownloads(), rag=Rag())
        self.assertEqual(legacy.tools['request_ingestion']('image', 'space')['revision'], 'v1')

    def test_image_search_returns_preview_and_filters_collection(self):
        class Rag:
            def list_spaces(self):
                return [{'id': 'space'}]
            def search_vector(self, space_id, vector, **kwargs):
                self.params = kwargs
                return {'results': [{'asset_id': 'photo', 'version_id': 'v1', 'kind': 'image'}]}
            def list_artifacts(self, asset_id):
                return [{'id': 'thumb', 'kind': 'thumbnail'}]
        class Encoder:
            def encode(self, query, selected):
                return [1, 0]
        class Downloads:
            def link(self, asset_id, version_id):
                return {'url': 'https://example.org/original'}
        class Artifact:
            def preview_link(self, artifact_id, version_id):
                return {'url': 'https://example.org/preview'}
        server, rag = FakeServer(), Rag()
        register_storage_tools(server, FakeRepository(), FakeUploads(), Downloads(), rag=rag,
                               encoder=Encoder(), artifact_downloads=Artifact())
        response = server.tools['search_assets']('바다', collection_id='trip')
        self.assertEqual(rag.params['collection_id'], 'trip')
        self.assertEqual(response['status'], 'ok')
        self.assertEqual(response['results'][0]['preview']['url'], 'https://example.org/preview')

    def test_tool_discovery_describes_available_features_and_rag_requirement(self):
        server = MCPServer('metadata-test')
        register_storage_tools(server, FakeRepository(), FakeUploads(), FakeDownloads(),
                               imports=object(), rag=object(), encoder=object(),
                               artifact_downloads=object())
        tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
        self.assertEqual(len(tools), 32)
        self.assertTrue(all(tool.title and tool.description for tool in tools.values()))
        self.assertEqual(tools['create_upload'].title, '사진·영상·파일 업로드')
        self.assertIn('제목', tools['list_assets'].description)
        self.assertIn('인코더', tools['search_assets'].description)
        self.assertIn('원본', tools['get_download_link'].description)
        self.assertIn('삭제', tools['delete_asset'].description)

    def test_tools_save_list_and_link_without_exposing_storage_paths(self):
        server = FakeServer()
        register_storage_tools(server, FakeRepository(), FakeUploads(), FakeDownloads())
        result = server.tools["save_text"]("제목", "안녕하세요")
        self.assertEqual(result["id"], "one")
        self.assertNotIn("object_key", result)
        self.assertNotIn("object_key", server.tools["list_assets"]()["assets"][0])
        self.assertIn("upload_url", server.tools["create_upload"]("영상", "video"))
        self.assertIn("url", server.tools["get_download_link"]("one"))


if __name__ == "__main__":
    unittest.main()
