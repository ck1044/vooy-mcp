"""Set a calibrated cosine threshold for one model space and one collection."""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vooy_storage.repository import Repository
from vooy_storage.rag import RAGService


def main():
    parser = argparse.ArgumentParser(description='컬렉션·임베딩 공간별 최소 검색 유사도를 설정합니다.')
    parser.add_argument('--space-id', required=True)
    parser.add_argument('--collection-id', required=True)
    parser.add_argument('--min-similarity', type=float, required=True)
    args = parser.parse_args()
    password_file = os.environ.get('VOOY_DATABASE_PASSWORD_FILE')
    password = Path(password_file).read_text().strip() if password_file else None
    repository = Repository(os.environ['VOOY_DATABASE_URL'], os.environ.get('VOOY_MEDIA_ROOT', '/data/media'), password)
    rag = RAGService(repository)
    rag.init_schema()
    print(json.dumps(rag.set_collection_search_policy(args.space_id, args.collection_id, args.min_similarity), ensure_ascii=False))


if __name__ == '__main__':
    main()
