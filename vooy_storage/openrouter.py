"""OpenRouter multimodal embeddings and a compatible MCP question encoder."""
from __future__ import annotations

import math
from urllib.parse import urlsplit

import httpx2


class OpenRouterEmbeddings:
    """Create compatible image and text vectors through OpenRouter.

    One image per request keeps the returned vector associated with one source.
    """

    endpoint = 'https://openrouter.ai/api/v1/embeddings'

    def __init__(self, api_key, model='google/gemini-embedding-2', dimensions=1536, provider=None):
        if not isinstance(api_key, str) or not api_key.strip() or any(c.isspace() for c in api_key):
            raise ValueError('OpenRouter API 키가 올바르지 않습니다.')
        if model != 'google/gemini-embedding-2':
            raise ValueError('지원하지 않는 OpenRouter 임베딩 모델입니다.')
        if isinstance(dimensions, bool) or not isinstance(dimensions, int) or not 128 <= dimensions <= 3072:
            raise ValueError('임베딩 차원은 128~3072여야 합니다.')
        if provider is not None and (not isinstance(provider, str) or not provider.strip()):
            raise ValueError('모델 제공자 이름이 올바르지 않습니다.')
        self.api_key = api_key
        self.model = model
        self.dimensions = dimensions
        self.provider = provider

    def _embed(self, input_value):
        body = {'model': self.model, 'dimensions': self.dimensions,
                'encoding_format': 'float', 'input': input_value}
        if self.provider:
            body['provider'] = {'only': [self.provider], 'allow_fallbacks': False,
                                'require_parameters': True}
        response = httpx2.post(self.endpoint, json=body,
                               headers={'Authorization': 'Bearer ' + self.api_key},
                               timeout=60, follow_redirects=False, trust_env=False)
        response.raise_for_status()
        payload = response.json()
        # OpenRouter may return the upstream model name without its provider
        # prefix, even though the requested route uses the full model ID.
        accepted_models = {self.model, 'gemini-embedding-2'}
        if not isinstance(payload, dict) or payload.get('model') not in accepted_models:
            raise ValueError('임베딩 응답 모델이 요청과 일치하지 않습니다.')
        data = payload.get('data')
        if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
            raise ValueError('임베딩 응답 개수가 올바르지 않습니다.')
        if data[0].get('index', 0) != 0:
            raise ValueError('임베딩 응답 순번이 올바르지 않습니다.')
        vector = data[0].get('embedding')
        if not isinstance(vector, list) or len(vector) != self.dimensions:
            raise ValueError('임베딩 응답 차원이 일치하지 않습니다.')
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in vector):
            raise ValueError('임베딩 벡터는 유한한 숫자여야 합니다.')
        norm = math.hypot(*(float(v) for v in vector))
        if not math.isfinite(norm) or norm == 0:
            raise ValueError('임베딩 벡터의 크기가 올바르지 않습니다.')
        return [float(v) / norm for v in vector]

    def embed_image(self, image_url):
        if not isinstance(image_url, str) or not image_url:
            raise ValueError('이미지 입력이 필요합니다.')
        valid_data = image_url.startswith(('data:image/jpeg;base64,', 'data:image/png;base64,'))
        parsed = urlsplit(image_url) if not valid_data else None
        valid_https = parsed is not None and parsed.scheme == 'https' and bool(parsed.hostname) and not parsed.username and not parsed.password
        if not valid_data and not valid_https:
            raise ValueError('이미지는 HTTPS URL 또는 JPEG/PNG data URI여야 합니다.')
        return self._embed([{'content': [{'type': 'image_url', 'image_url': {'url': image_url}}]}])

    def embed_query(self, query):
        if not isinstance(query, str) or not query.strip() or len(query.encode('utf-8')) > 65536:
            raise ValueError('검색 질문은 64 KiB 이하의 텍스트여야 합니다.')
        return self._embed(f'task: search result | query: {query.strip()}')

    def embed_document(self, text, title=None):
        if not isinstance(text, str) or not text.strip() or len(text.encode('utf-8')) > 65536:
            raise ValueError('검색 문서는 64 KiB 이하의 텍스트여야 합니다.')
        if title is not None and (not isinstance(title, str) or len(title.encode('utf-8')) > 1024):
            raise ValueError('문서 제목이 올바르지 않습니다.')
        return self._embed(f'title: {title.strip() if title and title.strip() else "none"} | text: {text.strip()}')


class OpenRouterQueryEncoder:
    """Encode questions only for the configured and compatible RAG space."""

    def __init__(self, embeddings):
        self.embeddings = embeddings

    def encode(self, query, space):
        if (not isinstance(space, dict) or space.get('model_id') != self.embeddings.model
                or space.get('dimensions') != self.embeddings.dimensions
                or space.get('representation') != 'single_vector'):
            raise ValueError('질문 인코더의 모델과 임베딩 공간이 일치하지 않습니다.')
        return self.embeddings.embed_query(query)
