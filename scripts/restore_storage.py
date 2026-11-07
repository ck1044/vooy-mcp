"""Restore a verified backup into a separate empty database and directory."""
from __future__ import annotations

import argparse
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys

from backup_storage import PROJECT_ROOT, hash_file, validate_backup, validate_references


def main():
    parser = argparse.ArgumentParser(description='Vooy 백업을 독립 테스트 DB와 빈 저장 위치에 복구합니다.')
    parser.add_argument('--backup', type=Path, required=True)
    parser.add_argument('--target-dir', type=Path, required=True)
    parser.add_argument('--target-db-container', required=True)
    parser.add_argument('--target-db-name', required=True, help='vooy_restore_ 접두사로 만든 빈 DB')
    parser.add_argument('--target-db-user', default='vooy')
    args = parser.parse_args()

    if not re.fullmatch(r'vooy_restore_[a-z0-9_]+', args.target_db_name):
        raise ValueError('복구 DB 이름은 vooy_restore_ 접두사와 영문 소문자·숫자·밑줄로 지정해 주세요.')
    if args.target_db_container == 'vooy-mcp-postgres-1':
        raise ValueError('운영 DB 컨테이너와 별개의 복구 컨테이너를 사용해 주세요.')
    source_container = subprocess.run(['docker', 'compose', 'ps', '-a', '-q', 'postgres'],
                                      cwd=PROJECT_ROOT, check=True, capture_output=True, text=True).stdout.strip()
    target_container = subprocess.run(['docker', 'inspect', '--format', '{{.Id}}', args.target_db_container],
                                      check=True, capture_output=True, text=True).stdout.strip()
    if source_container and target_container == source_container:
        raise ValueError('운영 DB 컨테이너와 별개의 복구 컨테이너를 사용해 주세요.')
    target = args.target_dir.resolve()
    if target.exists():
        raise ValueError('복구 위치는 아직 존재하지 않는 디렉터리여야 합니다.')
    backup = args.backup.resolve(strict=True)
    if target.is_relative_to(backup):
        raise ValueError('복구 위치는 백업 디렉터리 밖이어야 합니다.')
    manifest = json.loads((backup / 'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('format') != 1 or manifest['database']['file'] != 'database.dump':
        raise ValueError('지원하지 않는 백업 형식입니다.')
    dump = backup / 'database.dump'
    if dump.is_symlink() or not dump.is_file() or dump.stat().st_size != manifest['database']['size'] or hash_file(dump) != manifest['database']['sha256']:
        raise ValueError('데이터베이스 백업의 크기 또는 해시가 다릅니다.')
    validate_backup(backup, manifest['files'])
    validate_references(manifest['files'], manifest.get('database_references', []))

    command = ['docker', 'exec', '-i', args.target_db_container]
    query = "SELECT count(*) FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname NOT LIKE 'pg_toast%' AND c.relkind IN ('r','p','v','m','f')"
    count = subprocess.run(command + ['psql', '-U', args.target_db_user, '-d', args.target_db_name,
                                     '-At', '-v', 'ON_ERROR_STOP=1', '-c', query],
                           check=True, capture_output=True, text=True).stdout.strip()
    if count != '0':
        raise ValueError('복구 대상 데이터베이스가 비어 있지 않습니다.')
    with dump.open('rb') as stream:
        subprocess.run(command + ['pg_restore', '-U', args.target_db_user, '-d', args.target_db_name,
                                  '--no-owner', '--no-privileges', '--exit-on-error'], stdin=stream, check=True)
    target.mkdir(parents=True, exist_ok=False)
    for entry in manifest['files']:
        rel = Path(*PurePosixPath(entry['path']).parts)
        destination = target / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(backup / 'media' / rel, destination)
        if hash_file(destination) != entry['sha256']:
            raise ValueError(f'복구 파일 해시가 다릅니다: {entry["path"]}')
    print(f'복구 완료: DB {args.target_db_name}, 미디어 {target}, 파일 {len(manifest["files"])}개')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, KeyError, json.JSONDecodeError, subprocess.CalledProcessError) as error:
        print(f'복구 실패: {error}', file=sys.stderr)
        sys.exit(1)
