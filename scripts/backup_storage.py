"""Create a consistent PostgreSQL and immutable media backup.

All Compose services except PostgreSQL are stopped for the duration of the
snapshot and automatically restarted, including on error.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import signal
import subprocess
import sys
import uuid

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MEDIA_DIRS = ('originals', 'derived', 'staging', 'imports')


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def copy_media(source: Path, backup: Path) -> list[dict]:
    source, backup = source.resolve(strict=True), backup.resolve(strict=True)
    if backup.is_relative_to(source):
        raise ValueError('백업 위치는 미디어 저장소 밖이어야 합니다.')
    result = []
    for name in MEDIA_DIRS:
        directory = source / name
        if directory.is_symlink():
            raise ValueError('심볼릭 링크는 백업할 수 없습니다.')
        if not directory.exists():
            continue
        for parent, dirs, files in os.walk(directory, followlinks=False):
            parent = Path(parent)
            for entry in dirs + files:
                if (parent / entry).is_symlink():
                    raise ValueError('심볼릭 링크는 백업할 수 없습니다.')
            for filename in sorted(files):
                source_file = parent / filename
                if not source_file.is_file():
                    raise ValueError('일반 파일만 백업할 수 있습니다.')
                rel = source_file.relative_to(source)
                target = backup / 'media' / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source_file, target)
                copied_sha256 = hash_file(target)
                if source_file.stat().st_size != target.stat().st_size or hash_file(source_file) != copied_sha256:
                    raise ValueError(f'원본과 백업 파일이 다릅니다: {rel.as_posix()}')
                result.append({'path': rel.as_posix(), 'size': target.stat().st_size, 'sha256': copied_sha256})
    return sorted(result, key=lambda item: item['path'])


def validate_backup(backup: Path, entries: list[dict]) -> None:
    backup = backup.resolve(strict=True)
    seen = set()
    for entry in entries:
        path = entry['path']
        if not isinstance(path, str):
            raise ValueError('백업에 잘못된 파일 경로가 있습니다.')
        rel = PurePosixPath(path)
        if (rel.is_absolute() or not rel.parts or str(rel) != path or
                rel.parts[0] not in MEDIA_DIRS or any(p in ('..', '.') for p in rel.parts) or path in seen):
            raise ValueError('백업에 잘못된 파일 경로가 있습니다.')
        seen.add(path)
        target = backup / 'media' / Path(*rel.parts)
        if target.is_symlink() or not target.is_file() or not target.resolve().is_relative_to(backup / 'media'):
            raise ValueError(f'백업 파일을 확인할 수 없습니다: {path}')
        if target.stat().st_size != entry['size'] or hash_file(target) != entry['sha256']:
            raise ValueError(f'백업 파일 해시가 다릅니다: {path}')


def validate_references(entries: list[dict], references: list[dict]) -> None:
    files = {entry['path']: entry for entry in entries}
    for reference in references:
        entry = files.get(reference['path'])
        if not entry or entry['size'] != reference['size'] or entry['sha256'] != reference['sha256']:
            raise ValueError(f'DB 자료와 백업 파일이 다릅니다: {reference["path"]}')


def compose(*args, **kwargs):
    return subprocess.run(['docker', 'compose', *args], cwd=PROJECT_ROOT, check=True, **kwargs)


def database_references():
    base = ('exec', '-T', 'postgres', 'psql', '-U', 'vooy', '-d', 'vooy', '-At', '-v', 'ON_ERROR_STOP=1')
    exists = compose(*base, '-c', "SELECT to_regclass('public.asset_versions') IS NOT NULL, to_regclass('public.assets') IS NOT NULL",
                     capture_output=True, text=True).stdout.strip()
    if exists.startswith('t|'):
        query = "SELECT json_build_object('path', v.object_key, 'size', v.size_bytes, 'sha256', trim(v.sha256)) FROM asset_versions v JOIN assets a ON a.id = v.asset_id WHERE a.deleted_at IS NULL AND v.object_key IS NOT NULL"
    elif exists.endswith('|t'):
        query = "SELECT json_build_object('path', object_key, 'size', size_bytes, 'sha256', trim(sha256)) FROM assets WHERE deleted_at IS NULL AND object_key IS NOT NULL"
    else:
        return []
    output = compose(*base, '-c', query, capture_output=True, text=True).stdout
    references = [json.loads(line) for line in output.splitlines() if line]
    rag_exists = compose(*base, '-c', "SELECT to_regclass('public.rag_artifacts') IS NOT NULL", capture_output=True, text=True).stdout.strip()
    if rag_exists == 't':
        query = """SELECT json_build_object('path', t.object_key, 'size', t.size_bytes, 'sha256', trim(t.sha256))
                   FROM rag_artifacts t JOIN rag_result_sets r ON r.id=t.result_set_id
                   JOIN assets a ON a.id=r.asset_id
                   WHERE t.state='ready' AND r.state='active' AND a.deleted_at IS NULL AND a.version_id=r.asset_version_id"""
        output = compose(*base, '-c', query, capture_output=True, text=True).stdout
        references.extend(json.loads(line) for line in output.splitlines() if line)
    return references


def main():
    parser = argparse.ArgumentParser(description='Vooy DB 및 미디어의 일관된 백업을 생성합니다.')
    parser.add_argument('--destination', type=Path, required=True, help='백업 디렉터리를 만들 상위 위치')
    parser.add_argument('--media-root', type=Path, default=Path('/mnt/d/VOOY/media'))
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(InterruptedError('백업이 중단되었습니다.')))
    state_dir = PROJECT_ROOT / '.secrets'
    state_dir.mkdir(mode=0o700, exist_ok=True)
    marker = state_dir / 'backup-maintenance.json'
    if marker.exists():
        previous = json.loads(marker.read_text(encoding='utf-8'))['services']
        if previous:
            compose('start', *previous)
        marker.unlink()
    args.destination.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    final = args.destination / f'vooy-backup-{stamp}-{uuid.uuid4().hex[:8]}'
    partial = final.with_name(final.name + '.partial')
    partial.mkdir(mode=0o700)
    running = compose('ps', '--services', '--status', 'running', capture_output=True, text=True).stdout.splitlines()
    services = [name for name in running if name != 'postgres']
    try:
        if services:
            marker_tmp = marker.with_suffix('.tmp')
            marker_tmp.write_text(json.dumps({'services': services}), encoding='utf-8')
            marker_tmp.chmod(0o600)
            os.replace(marker_tmp, marker)
            compose('stop', *services)
        dump = partial / 'database.dump'
        with dump.open('xb') as out:
            compose('exec', '-T', 'postgres', 'pg_dump', '-U', 'vooy', '-d', 'vooy',
                    '--format=custom', '--no-owner', '--no-privileges', stdout=out)
        entries = copy_media(args.media_root, partial)
        references = database_references()
        validate_references(entries, references)
        manifest = {'format': 1, 'created_at': datetime.now(timezone.utc).isoformat(),
                    'database': {'file': 'database.dump', 'sha256': hash_file(dump), 'size': dump.stat().st_size},
                    'files': entries, 'database_references': references}
        (partial / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
        validate_backup(partial, entries)
        os.replace(partial, final)
        print(f'백업 완료: {final} (파일 {len(entries)}개)')
    finally:
        if services:
            compose('start', *services)
            marker.unlink(missing_ok=True)


if __name__ == '__main__':
    try:
        state_dir = PROJECT_ROOT / '.secrets'
        state_dir.mkdir(mode=0o700, exist_ok=True)
        with (state_dir / 'backup.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            main()
    except (ValueError, OSError, KeyError, json.JSONDecodeError, subprocess.CalledProcessError) as error:
        print(f'백업 실패: {error}', file=sys.stderr)
        sys.exit(1)
