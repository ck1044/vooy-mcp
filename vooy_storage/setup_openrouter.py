"""Register the configured OpenRouter embedding space without processing data."""
import os
from pathlib import Path

from vooy_storage.repository import Repository
from vooy_storage.rag import RAGService


def main():
    password_file = os.environ.get('VOOY_DATABASE_PASSWORD_FILE')
    password = Path(password_file).read_text(encoding='utf-8').strip() if password_file else None
    repository = Repository(os.environ['VOOY_DATABASE_URL'], os.environ.get('VOOY_MEDIA_ROOT', '/data/media'), password)
    repository.init_schema()
    rag = RAGService(repository)
    rag.init_schema()
    model = os.environ.get('VOOY_EMBEDDING_MODEL', 'google/gemini-embedding-2')
    dimensions = int(os.environ.get('VOOY_EMBEDDING_DIMENSIONS', '1536'))
    provider = os.environ.get('VOOY_EMBEDDING_PROVIDER') or 'openrouter-default'
    space = rag.register_space(model_id=model, model_revision=provider, dimensions=dimensions,
        metric='cosine', normalization='unit', preprocessing_revision='image-jpeg-v1',
        modalities=['image', 'text', 'video', 'audio', 'document'], representation='single_vector',
        document_encoder='openrouter-image-document-v1', query_encoder='openrouter-search-query-v1')
    print(space['id'])


if __name__ == '__main__':
    main()
