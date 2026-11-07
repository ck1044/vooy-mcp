"""Import images from a mounted directory without modifying the source files."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vooy_storage.repository import Repository
from vooy_storage.uploads import file_type

NAMESPACE = uuid.UUID('436a0777-51d3-4c5e-b59c-1a5168a9d115')
EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.heic', '.heif'}
TYPES = {'image/jpeg', 'image/png', 'image/webp', 'image/gif', 'image/heic'}


def import_directory(repository, directory, dataset='default', emit=print):
    root = Path(directory).resolve(strict=True)
    media_root = Path(repository.media_root).resolve()
    if not root.is_dir():
        raise ValueError('이미지 경로는 폴더여야 합니다.')
    if root.is_relative_to(media_root) or media_root.is_relative_to(root):
        raise ValueError('이미지 폴더와 미디어 저장소가 서로 포함되어 있습니다.')
    if not isinstance(dataset, str) or not dataset.strip() or len(dataset) > 256:
        raise ValueError('데이터셋 이름을 입력하세요.')
    counts = {'imported': 0, 'existing': 0, 'unsupported': 0, 'failed': 0}
    def walk_error(exc):
        counts['failed'] += 1
        emit(json.dumps({'status': 'failed', 'relative_path': str(exc.filename), 'error': str(exc)}, ensure_ascii=False))
    for parent, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
        dirs[:] = sorted(d for d in dirs if not (Path(parent) / d).is_symlink())
        for name in sorted(files):
            path = Path(parent) / name
            rel = path.relative_to(root).as_posix()
            if path.is_symlink() or not path.is_file() or path.suffix.lower() not in EXTENSIONS:
                counts['unsupported'] += 1
                continue
            try:
                mime = file_type(path, name)
                if mime not in TYPES:
                    raise ValueError('이미지 형식이 아닙니다.')
                digest = hashlib.sha256()
                size = 0
                with path.open('rb') as stream:
                    while chunk := stream.read(1024 * 1024):
                        size += len(chunk)
                        digest.update(chunk)
                sha = digest.hexdigest()
                key = json.dumps([dataset, rel, sha], ensure_ascii=False, separators=(',', ':'))
                asset_id = str(uuid.uuid5(NAMESPACE, key))
                prior = repository.get_asset(asset_id)
                asset = repository.save_file(path.stem, name, mime, path, size, sha, asset_id=asset_id)
                if not asset.get('source'):
                    repository.update_asset(asset_id, source='folder:' + dataset + '/' + rel)
                outcome = 'existing' if prior else 'imported'
                counts[outcome] += 1
                emit(json.dumps({'status': outcome, 'relative_path': rel, 'asset_id': asset_id,
                                 'sha256': sha, 'size_bytes': size}, ensure_ascii=False))
            except (OSError, ValueError) as exc:
                counts['failed'] += 1
                emit(json.dumps({'status': 'failed', 'relative_path': rel, 'error': str(exc)}, ensure_ascii=False))
    return counts


def main():
    parser = argparse.ArgumentParser(description='폴더의 이미지를 원본을 유지하며 Vooy 저장소에 등록합니다.')
    parser.add_argument('directory', help='컨테이너에 읽기 전용으로 연결한 이미지 폴더')
    parser.add_argument('--dataset', default='default', help='반복 등록을 구분하는 데이터셋 이름')
    args = parser.parse_args()
    password_file = os.environ.get('VOOY_DATABASE_PASSWORD_FILE')
    password = Path(password_file).read_text(encoding='utf-8').strip() if password_file else None
    repository = Repository(os.environ['VOOY_DATABASE_URL'], os.environ.get('VOOY_MEDIA_ROOT', '/data/media'), password)
    summary = import_directory(repository, args.directory, args.dataset)
    print(json.dumps({'summary': summary}, ensure_ascii=False))
    return 1 if summary['failed'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
