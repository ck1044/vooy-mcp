"""Evaluate direct image vectors with OBA's reviewed visual-only questions.

The report contains question IDs and result scores, but no question text or API key.
No threshold or model decision is applied to the nearest-neighbour results.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vooy_storage.openrouter import OpenRouterEmbeddings
from vooy_storage.rag import RAGService
from vooy_storage.repository import Repository


def visual_questions(path):
    """Use reviewed questions with no synthetic location/date/identity filter."""
    rows = []
    for number, line in enumerate(Path(path).read_text(encoding='utf-8').splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict) or row.get('reviewed') is not True:
            raise ValueError(f'평가 문항 {number}이 검수되지 않았습니다.')
        if row.get('split') not in ('dev', 'test') or row.get('expected_status') not in ('ok', 'no_match'):
            raise ValueError(f'평가 문항 {number}의 분할 또는 상태가 올바르지 않습니다.')
        filters = row.get('expected_filters') or {}
        if not isinstance(filters, dict):
            raise ValueError(f'평가 문항 {number}의 필터가 올바르지 않습니다.')
        if any(value not in (None, '', [], {}) for value in filters.values()):
            continue
        if not isinstance(row.get('query'), str) or not row['query'].strip():
            raise ValueError(f'평가 문항 {number}에 질문이 없습니다.')
        if not isinstance(row.get('id'), str) or not isinstance(row.get('relevant_photo_ids'), list):
            raise ValueError(f'평가 문항 {number}의 정답 ID가 올바르지 않습니다.')
        if row['expected_status'] == 'ok' and not row['relevant_photo_ids']:
            raise ValueError(f'평가 문항 {number}에 정답 사진이 없습니다.')
        if row['expected_status'] == 'no_match' and row['relevant_photo_ids']:
            raise ValueError(f'평가 문항 {number}의 정답 없음 상태가 일치하지 않습니다.')
        rows.append(row)
    ids = [row['id'] for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError('중복된 평가 문항 ID가 있습니다.')
    return rows


def corpus(repository, dataset, space_id, expected_images):
    with repository.connect() as conn:
        collection = conn.execute('SELECT id FROM collections WHERE name=%s', (dataset,)).fetchone()
        if not collection:
            raise ValueError('평가 데이터셋 컬렉션을 찾을 수 없습니다.')
        rows = conn.execute('''SELECT o.photo_id, o.asset_id, a.version_id,
            EXISTS(SELECT 1 FROM rag_result_sets r JOIN rag_segments s ON s.result_set_id=r.id
                   JOIN rag_embeddings e ON e.segment_id=s.id AND e.result_set_id=r.id
                   WHERE r.asset_id=a.id AND r.asset_version_id=a.version_id AND r.space_id=%s
                     AND r.state='active' AND s.kind='image') AS indexed
            FROM oba_import_metadata o JOIN assets a ON a.id=o.asset_id AND a.deleted_at IS NULL
            JOIN asset_collections ac ON ac.asset_id=a.id AND ac.collection_id=%s
            WHERE o.dataset=%s AND a.sha256=o.sha256 ORDER BY o.photo_id''',
            (space_id, collection['id'], dataset)).fetchall()
    if len(rows) != expected_images or len({row['photo_id'] for row in rows}) != expected_images:
        raise ValueError(f'평가 사진 수 또는 ID가 일치하지 않습니다: {len(rows)}/{expected_images}')
    if not all(row['indexed'] for row in rows):
        raise ValueError(f'색인 미완료: {sum(not row["indexed"] for row in rows)}장')
    return str(collection['id']), {str(row['asset_id']): row['photo_id'] for row in rows}


def evaluate(rows, encode, search, asset_photo_ids):
    results = []
    summaries = {}
    for row in rows:
        vector = encode(row['query'])
        hits = search(vector)
        candidates = []
        relevant = set(row['relevant_photo_ids'])
        for hit in hits:
            photo_id = asset_photo_ids.get(str(hit['asset_id']))
            if photo_id is None:
                raise ValueError('검색 결과가 평가 데이터셋에 속하지 않습니다.')
            distance = float(hit['distance'])
            candidates.append({'photo_id': photo_id, 'cosine_distance': distance,
                               'cosine_similarity': 1.0 - distance, 'is_labeled_relevant': photo_id in relevant})
        found = any(item['is_labeled_relevant'] for item in candidates)
        results.append({'question_id': row['id'], 'split': row['split'],
                        'question_type': 'visual_match' if row['expected_status'] == 'ok' else 'visual_no_match',
                        'expected_status': row['expected_status'], 'hit_at_3': found if relevant else None,
                        'candidates': candidates})
    for split in ('dev', 'test'):
        entries = [r for r in results if r['split'] == split]
        positives = [r for r in entries if r['expected_status'] == 'ok']
        negatives = [r for r in entries if r['expected_status'] == 'no_match']
        summaries[split] = {'questions': len(entries), 'visual_match_questions': len(positives),
            'hit_at_3_count': sum(r['hit_at_3'] for r in positives),
            'hit_at_3': (sum(r['hit_at_3'] for r in positives) / len(positives)) if positives else None,
            'visual_no_match_questions': len(negatives),
            'negative_questions_returning_candidates_without_threshold': sum(bool(r['candidates']) for r in negatives)}
    return {'threshold_applied': False, 'description': '직접 이미지 벡터의 상위 후보 점수; no_match 판정은 수행하지 않음',
            'splits': summaries, 'results': results}


def main():
    parser = argparse.ArgumentParser(description='OBA 직접 이미지 검색의 검수된 시각 질의 평가')
    parser.add_argument('questions', help='OBA bts_daily_queries.jsonl 경로')
    parser.add_argument('--dataset', default='bts-daily')
    parser.add_argument('--space-id', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--expected-images', type=int, default=100)
    args = parser.parse_args()
    password_file = os.environ.get('VOOY_DATABASE_PASSWORD_FILE')
    password = Path(password_file).read_text(encoding='utf-8').strip() if password_file else None
    repository = Repository(os.environ['VOOY_DATABASE_URL'], os.environ.get('VOOY_MEDIA_ROOT', '/data/media'), password)
    rag = RAGService(repository)
    key = Path(os.environ['VOOY_OPENROUTER_API_KEY_FILE']).read_text(encoding='utf-8').strip()
    model = os.environ.get('VOOY_EMBEDDING_MODEL', 'google/gemini-embedding-2')
    dimensions = int(os.environ.get('VOOY_EMBEDDING_DIMENSIONS', '1536'))
    embeddings = OpenRouterEmbeddings(key, model=model, dimensions=dimensions,
                                       provider=os.environ.get('VOOY_EMBEDDING_PROVIDER') or None)
    spaces = [space for space in rag.list_spaces() if space['id'] == args.space_id]
    if not spaces or spaces[0]['model_id'] != model or spaces[0]['dimensions'] != dimensions or spaces[0]['metric'] != 'cosine':
        raise ValueError('평가 모델·차원·거리와 임베딩 공간이 일치하지 않습니다.')
    collection_id, mapping = corpus(repository, args.dataset, args.space_id, args.expected_images)
    rows = visual_questions(args.questions)
    known = set(mapping.values())
    if any(not set(row['relevant_photo_ids']).issubset(known) for row in rows):
        raise ValueError('평가 문항의 정답이 등록된 데이터셋에 없습니다.')
    report = evaluate(rows, embeddings.embed_query,
                      lambda vector: rag.search_vector(args.space_id, vector, limit=3, kinds=['image'],
                                                       collection_id=collection_id)['results'], mapping)
    report.update(dataset=args.dataset, model=model, dimensions=dimensions, corpus_size=len(mapping))
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'output': args.output, 'splits': report['splits']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
