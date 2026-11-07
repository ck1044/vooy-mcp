import math
import unittest
from unittest.mock import patch

from vooy_storage.openrouter import OpenRouterEmbeddings, OpenRouterQueryEncoder


class Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class OpenRouterTests(unittest.TestCase):
    def setUp(self):
        self.client = OpenRouterEmbeddings('test-key', dimensions=128)
        self.vector = [3.0, 4.0] + [0.0] * 126
        self.payload = {'model': self.client.model, 'data': [{'index': 0, 'embedding': self.vector}]}

    @patch('vooy_storage.openrouter.httpx2.post')
    def test_image_is_sent_as_single_image_input_and_normalized(self, post):
        post.return_value = Response(self.payload)
        image = 'data:image/jpeg;base64,YWJj'
        values = self.client.embed_image(image)
        self.assertEqual(values[:2], [0.6, 0.8])
        body = post.call_args.kwargs['json']
        self.assertEqual(body['input'], [{'content': [{'type': 'image_url', 'image_url': {'url': image}}]}])
        self.assertEqual(body['dimensions'], 128)
        self.assertEqual(post.call_args.kwargs['headers']['Authorization'], 'Bearer test-key')
        self.assertFalse(post.call_args.kwargs['follow_redirects'])

    @patch('vooy_storage.openrouter.httpx2.post')
    def test_query_and_document_use_model_retrieval_formats(self, post):
        post.return_value = Response(self.payload)
        self.client.embed_query('제주 여행')
        self.assertEqual(post.call_args.kwargs['json']['input'], 'task: search result | query: 제주 여행')
        self.client.embed_document('바다 사진', '제주')
        self.assertEqual(post.call_args.kwargs['json']['input'], 'title: 제주 | text: 바다 사진')

    @patch('vooy_storage.openrouter.httpx2.post')
    def test_accepts_upstream_model_name_returned_by_openrouter(self, post):
        post.return_value = Response({'model': 'gemini-embedding-2',
                                      'data': [{'index': 0, 'embedding': self.vector}]})
        self.assertEqual(self.client.embed_query('여행')[:2], [0.6, 0.8])

    @patch('vooy_storage.openrouter.httpx2.post')
    def test_rejects_wrong_model_dimension_nonfinite_and_zero(self, post):
        for payload in (
            {'model': 'wrong', 'data': [{'embedding': self.vector}]},
            {'model': self.client.model, 'data': [{'embedding': self.vector[:-1]}]},
            {'model': self.client.model, 'data': [{'embedding': [math.nan] + self.vector[1:]}]},
            {'model': self.client.model, 'data': [{'embedding': [0] * 128}]},
            {'model': self.client.model, 'data': [{'index': 1, 'embedding': self.vector}]},
        ):
            post.return_value = Response(payload)
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.client.embed_query('사진')

    def test_rejects_mismatched_space_before_call(self):
        encoder = OpenRouterQueryEncoder(self.client)
        with patch.object(self.client, 'embed_query') as query:
            with self.assertRaises(ValueError):
                encoder.encode('바다', {'model_id': self.client.model, 'dimensions': 256,
                                      'representation': 'single_vector'})
            query.assert_not_called()
            encoder.encode('바다', {'model_id': self.client.model, 'dimensions': 128,
                                  'representation': 'single_vector'})
            query.assert_called_once_with('바다')

    def test_rejects_untrusted_image_input_scheme(self):
        with self.assertRaises(ValueError):
            self.client.embed_image('http://127.0.0.1/image')
        with self.assertRaises(ValueError):
            self.client.embed_image('file:///etc/passwd')


if __name__ == '__main__':
    unittest.main()
