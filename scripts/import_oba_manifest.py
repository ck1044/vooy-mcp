"""Import OBA image manifest entries while preserving their provenance."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from psycopg.types.json import Jsonb
from vooy_storage.repository import Repository
from vooy_storage.uploads import file_type

NAMESPACE = uuid.UUID('75147e60-5200-45a0-95ac-2431118bed1e')
SUPPORTED = {'image/jpeg', 'image/png', 'image/webp', 'image/gif', 'image/heic'}


def init_schema(repository):
    with repository.connect() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS oba_import_metadata (
            asset_id uuid PRIMARY KEY REFERENCES assets(id),
            dataset text NOT NULL, photo_id text NOT NULL, gallery_id text,
            sha256 text NOT NULL, source jsonb NOT NULL, identities jsonb NOT NULL,
            metadata jsonb NOT NULL, raw_exif jsonb NOT NULL, manifest jsonb NOT NULL,
            updated_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE(dataset, photo_id, sha256))''')


def _metadata(repository, asset_id, dataset, record):
    with repository.connect() as conn:
        conn.execute('''INSERT INTO oba_import_metadata
            (asset_id, dataset, photo_id, gallery_id, sha256, source, identities, metadata, raw_exif, manifest)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (asset_id) DO UPDATE SET source=excluded.source,
              identities=excluded.identities, metadata=excluded.metadata, raw_exif=excluded.raw_exif,
              manifest=excluded.manifest, gallery_id=excluded.gallery_id, updated_at=now()''',
            (asset_id, dataset, record['photo_id'], record.get('gallery_id'), record['sha256'].lower(),
             Jsonb(record.get('source') or {}), Jsonb(record.get('identities') or []),
             Jsonb(record.get('metadata') or {}), Jsonb(record.get('raw_exif') or {}), Jsonb(record)))


def _dataset_collection(repository, dataset):
    # Concurrent importer processes for the same dataset must reuse one collection.
    with repository.connect() as conn:
        conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))', ('oba-collection:' + dataset,))
        rows = conn.execute('SELECT id FROM collections WHERE name=%s ORDER BY id LIMIT 2', (dataset,)).fetchall()
        if len(rows) > 1:
            raise ValueError('같은 이름의 컬렉션이 여러 개 있어 선택할 수 없습니다: ' + dataset)
        if rows:
            return str(rows[0]['id'])
        collection_id = uuid.uuid4()
        conn.execute('INSERT INTO collections (id,name,description) VALUES (%s,%s,%s)',
                     (collection_id, dataset, 'OBA 이미지 데이터셋'))
        return str(collection_id)


def import_manifest(repository, image_root, records, dataset, emit=print):
    root = Path(image_root).resolve(strict=True)
    if not root.is_dir():
        raise ValueError('이미지 기준 경로는 폴더여야 합니다.')
    if not isinstance(dataset, str) or not dataset.strip() or len(dataset.strip()) > 200:
        raise ValueError('데이터셋 이름이 올바르지 않습니다.')
    dataset = dataset.strip()
    if not isinstance(records, list):
        raise ValueError('manifest는 항목 배열이어야 합니다.')
    media_root = Path(repository.media_root).resolve()
    if root.is_relative_to(media_root) or media_root.is_relative_to(root):
        raise ValueError('원본 폴더와 저장소가 겹칩니다.')
    init_schema(repository)
    collection_id = _dataset_collection(repository, dataset)
    result = {'imported': 0, 'existing': 0, 'failed': 0}
    for record in records:
        photo_id = record.get('photo_id') if isinstance(record, dict) else None
        try:
            if not isinstance(photo_id, str) or not photo_id.strip() or len(photo_id) > 512:
                raise ValueError('photo_id가 올바르지 않습니다.')
            # OBA's selected manifest uses "file"; its full ingest manifest uses "image_path".
            relative = record.get('image_path') or record.get('file')
            if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
                raise ValueError('상대 이미지 경로가 필요합니다.')
            path = (root / relative).resolve(strict=True)
            if not path.is_relative_to(root) or not path.is_file():
                raise ValueError('이미지 경로가 기준 폴더 밖입니다.')
            declared = record.get('sha256')
            if not isinstance(declared, str) or not re.fullmatch(r'[0-9a-fA-F]{64}', declared):
                raise ValueError('SHA-256이 올바르지 않습니다.')
            digest = hashlib.sha256()
            size = 0
            with path.open('rb') as f:
                while chunk := f.read(1024 * 1024):
                    size += len(chunk)
                    digest.update(chunk)
            if digest.hexdigest() != declared.lower():
                raise ValueError('이미지 SHA-256이 manifest와 일치하지 않습니다.')
            mime = file_type(path, path.name)
            if mime not in SUPPORTED or (record.get('mime_type') and record['mime_type'] != mime):
                raise ValueError('이미지 형식이 manifest와 일치하지 않습니다.')
            for key, expected in [('source',dict), ('identities',list), ('metadata',dict), ('raw_exif',dict)]:
                if key in record and record[key] is not None and not isinstance(record[key],expected):
                    raise ValueError(f'{key} 형식이 올바르지 않습니다.')
            identity = json.dumps([dataset, photo_id, declared.lower()], ensure_ascii=False, separators=(',', ':'))
            asset_id = str(uuid.uuid5(NAMESPACE, identity))
            prior = repository.get_asset(asset_id)
            source = record.get('source') or {}
            title = source.get('title') if isinstance(source.get('title'), str) else None
            repository.save_file(title or path.stem, path.name, mime, path, size, declared, asset_id=asset_id)
            _metadata(repository, asset_id, dataset, record)
            if not repository.add_to_collection(collection_id, asset_id):
                raise ValueError('자료를 데이터셋 컬렉션에 연결할 수 없습니다.')
            outcome = 'existing' if prior else 'imported'
            result[outcome] += 1
            emit(json.dumps({'status': outcome, 'photo_id': photo_id, 'asset_id': asset_id}, ensure_ascii=False))
        except (OSError, ValueError, TypeError, KeyError) as exc:
            result['failed'] += 1
            emit(json.dumps({'status': 'failed', 'photo_id': photo_id, 'error': str(exc)}, ensure_ascii=False))
    return result


def main():
    parser = argparse.ArgumentParser(description='OBA manifest의 원본 사진과 메타데이터를 등록합니다.')
    parser.add_argument('manifest')
    parser.add_argument('--image-root', required=True)
    parser.add_argument('--dataset', required=True)
    args = parser.parse_args()
    records = json.loads(Path(args.manifest).read_text(encoding='utf-8'))
    password_file = os.environ.get('VOOY_DATABASE_PASSWORD_FILE')
    password = Path(password_file).read_text().strip() if password_file else None
    repo = Repository(os.environ['VOOY_DATABASE_URL'], os.environ.get('VOOY_MEDIA_ROOT', '/data/media'), password)
    repo.init_schema()
    summary = import_manifest(repo, args.image_root, records, args.dataset)
    print(json.dumps({'summary': summary}, ensure_ascii=False))
    return int(summary['failed'] > 0)


if __name__ == '__main__':
    raise SystemExit(main())
