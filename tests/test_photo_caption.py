import hashlib
import json
import unittest
from unittest.mock import patch

import httpx2

from vooy_storage.photo_caption import PhotoCaptioner


def response(description):
    return httpx2.Response(200, json={'choices': [{'message': {'content':
        json.dumps({'observations': [{'index': 0, 'description': description}]}, ensure_ascii=False)}}]},
        request=httpx2.Request('POST', 'https://openrouter.ai/api/v1/chat/completions'))


class PhotoCaptionTests(unittest.TestCase):
    def setUp(self):
        self.model = PhotoCaptioner('test-key')

    def test_result_is_bound_to_exact_photo_version_and_image(self):
        with patch('vooy_storage.photo_caption.httpx2.post', return_value=response('빨간 옷을 입은 사람이 서 있습니다.')) as post:
            a = self.model.describe('photo-a', 'v1', b'image-one')
            again = self.model.describe('photo-a', 'v1', b'image-one')
            b = self.model.describe('photo-b', 'v1', b'image-one')
            changed = self.model.describe('photo-a', 'v2', b'image-two')
        self.assertEqual(post.call_count, 3)
        self.assertEqual(a, again)
        self.assertEqual(a['photo_id'], 'photo-a')
        self.assertEqual(b['photo_id'], 'photo-b')
        self.assertEqual(changed['version_id'], 'v2')
        self.assertEqual(a['image_sha256'], hashlib.sha256(b'image-one').hexdigest())
        self.assertEqual(a['source'], 'ai_visual_observation')
        self.assertFalse(a['verified'])
        request_body = post.call_args.kwargs['json']
        self.assertEqual(request_body['model'], 'google/gemini-3.1-flash-lite')
        self.assertIn('신원', request_body['messages'][0]['content'][0]['text'])

    def test_provider_failure_or_bad_json_does_not_claim_description(self):
        for bad in (httpx2.Response(200, json={'choices': []},
                    request=httpx2.Request('POST', 'https://openrouter.ai/api/v1/chat/completions')),
                    httpx2.Response(200, json={'choices': [{'message': {'content': 'not-json'}}]},
                    request=httpx2.Request('POST', 'https://openrouter.ai/api/v1/chat/completions')),
                    httpx2.Response(429, request=httpx2.Request('POST', 'https://openrouter.ai/api/v1/chat/completions'))):
            with patch('vooy_storage.photo_caption.httpx2.post', return_value=bad):
                value = self.model.describe('asset', 'v1', b'image')
            self.assertEqual(value['status'], 'unavailable')
            self.assertIsNone(value['description'])

    def test_unavailable_is_not_cached_and_can_recover(self):
        bad = httpx2.Response(503, request=httpx2.Request('POST', 'https://openrouter.ai/api/v1/chat/completions'))
        with patch('vooy_storage.photo_caption.httpx2.post', side_effect=[bad, response('푸른 바다가 보입니다.')]):
            self.assertEqual(self.model.describe('a', 'v1', b'image')['status'], 'unavailable')
            self.assertEqual(self.model.describe('a', 'v1', b'image')['status'], 'available')

    def test_three_images_single_request_map_by_explicit_index(self):
        content = json.dumps({'observations': [
            {'index': 2, 'description': '초록색 사물'}, {'index': 0, 'description': '붉은 사물'},
            {'index': 1, 'description': '푸른 사물'}]}, ensure_ascii=False)
        mock = httpx2.Response(200, json={'choices': [{'message': {'content': content}}]},
                               request=httpx2.Request('POST', 'https://openrouter.ai/api/v1/chat/completions'))
        photos = [{'photo_id': str(i), 'version_id': 'v1', 'image_bytes': bytes([i+1])} for i in range(3)]
        with patch('vooy_storage.photo_caption.httpx2.post', return_value=mock) as post:
            result = self.model.describe_many(photos)
            self.assertEqual(post.call_count, 1)
            self.assertEqual([x['description'] for x in result], ['붉은 사물', '푸른 사물', '초록색 사물'])
            self.assertEqual([x['photo_id'] for x in result], ['0', '1', '2'])
            self.assertEqual(len(post.call_args.kwargs['json']['messages'][0]['content']), 7)

    def test_mismatched_or_duplicate_indices_do_not_attach_descriptions(self):
        content = json.dumps({'observations': [{'index': 0, 'description': 'one'},
                                              {'index': 0, 'description': 'two'}]})
        mock = httpx2.Response(200, json={'choices': [{'message': {'content': content}}]},
                               request=httpx2.Request('POST', 'https://openrouter.ai/api/v1/chat/completions'))
        photos = [{'photo_id': str(i), 'version_id': 'v1', 'image_bytes': bytes([i+1])} for i in range(2)]
        with patch('vooy_storage.photo_caption.httpx2.post', return_value=mock):
            result = self.model.describe_many(photos)
        self.assertEqual([x['status'] for x in result], ['unavailable', 'unavailable'])

    def test_truncated_batch_retries_individually_without_cross_mapping(self):
        import base64
        photos = [{'photo_id': str(i), 'version_id': 'v1', 'image_bytes': bytes([i+1])} for i in range(3)]
        def transport(*args, **kwargs):
            content = kwargs['json']['messages'][0]['content']
            if len(content) > 3:
                return httpx2.Response(200, json={'choices': [{'finish_reason': 'length',
                    'message': {'content': '{"observations":['}}]},
                    request=httpx2.Request('POST', 'https://openrouter.ai/api/v1/chat/completions'))
            encoded = content[-1]['image_url']['url'].split(',', 1)[1]
            value = base64.b64decode(encoded)[0]
            return response(f'사진 {value}')
        with patch('vooy_storage.photo_caption.httpx2.post', side_effect=transport) as post:
            result = self.model.describe_many(photos)
        self.assertEqual(post.call_count, 4)
        self.assertEqual([item['description'] for item in result], ['사진 1', '사진 2', '사진 3'])
        self.assertEqual([item['photo_id'] for item in result], ['0', '1', '2'])

    def test_unavailable_service_does_not_start_additional_requests(self):
        bad = httpx2.Response(503, request=httpx2.Request('POST', 'https://openrouter.ai/api/v1/chat/completions'))
        photos = [{'photo_id': str(i), 'version_id': 'v1', 'image_bytes': bytes([i+1])} for i in range(3)]
        with patch('vooy_storage.photo_caption.httpx2.post', return_value=bad) as post:
            result = self.model.describe_many(photos)
        self.assertEqual(post.call_count, 1)
        self.assertEqual([item['status'] for item in result], ['unavailable'] * 3)


if __name__ == '__main__':
    unittest.main()
