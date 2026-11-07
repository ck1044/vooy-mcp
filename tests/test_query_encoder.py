import unittest
from unittest.mock import patch

from vooy_storage.query_encoder import QueryEncoder


class Response:
    def raise_for_status(self):
        pass
    def json(self):
        return {'embedding': [1.0, 0.0, 0.0]}


class QueryEncoderTests(unittest.TestCase):
    def test_requires_configured_query_service(self):
        with self.assertRaisesRegex(ValueError, '인코더'):
            QueryEncoder(None).encode('바다', {'id': 'test'})

    @patch('vooy_storage.query_encoder.httpx2.post')
    def test_sends_question_and_space_to_encoder(self, post):
        post.return_value = Response()
        result = QueryEncoder('https://rag.example/encode', 'secret').encode('바다', {'id': 'space-1'})
        self.assertEqual(result, [1.0, 0.0, 0.0])
        self.assertEqual(post.call_args.kwargs['json'], {'query': '바다', 'space_id': 'space-1'})
        self.assertEqual(post.call_args.kwargs['headers']['Authorization'], 'Bearer secret')


if __name__ == '__main__':
    unittest.main()
