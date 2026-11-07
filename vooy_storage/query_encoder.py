"""Adapter for an independently deployed question embedding service."""
from urllib.parse import urlsplit

import httpx2


class QueryEncoder:
    def __init__(self, url, token=None):
        if url and (urlsplit(url).scheme not in ('http', 'https') or not urlsplit(url).hostname):
            raise ValueError('인코더 주소는 HTTP(S) 주소여야 합니다.')
        self.url = url
        self.token = token

    def encode(self, query, space):
        if not self.url:
            raise ValueError('질문 임베딩 인코더가 아직 연결되지 않았습니다.')
        if not isinstance(query, str) or not query.strip() or len(query.encode('utf-8')) > 65536:
            raise ValueError('검색 질문은 64 KiB 이하의 텍스트여야 합니다.')
        headers = {'Authorization': 'Bearer ' + self.token} if self.token else {}
        response = httpx2.post(self.url, json={'query': query, 'space_id': space['id']}, headers=headers,
                               timeout=30, follow_redirects=False, trust_env=False)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get('embedding'), list):
            raise ValueError('인코더 응답에 임베딩 벡터가 없습니다.')
        return payload['embedding']
