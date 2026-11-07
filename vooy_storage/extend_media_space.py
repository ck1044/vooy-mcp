"""Enable additional media types in the configured shared Gemini space."""
import os
from pathlib import Path

from vooy_storage.rag import RAGService
from vooy_storage.repository import Repository


def main():
    password_file = os.environ.get('VOOY_DATABASE_PASSWORD_FILE')
    password = Path(password_file).read_text(encoding='utf-8').strip() if password_file else None
    repo = Repository(os.environ['VOOY_DATABASE_URL'], os.environ.get('VOOY_MEDIA_ROOT', '/data/media'), password)
    rag = RAGService(repo)
    rag.init_schema()
    space = rag.extend_gemini_modalities(os.environ['VOOY_EMBEDDING_SPACE_ID'])
    print('사용 가능한 자료 유형:', ', '.join(space['modalities']))


if __name__ == '__main__':
    main()
