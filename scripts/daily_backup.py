"""Scheduled local backup with space checks and persistent status."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

try:
    from .backup_storage import MEDIA_DIRS, PROJECT_ROOT
except ImportError:
    from backup_storage import MEDIA_DIRS, PROJECT_ROOT

DEFAULT_DESTINATION = Path('/mnt/c/Users/PC/VOOY/backups')
DEFAULT_MEDIA = Path('/mnt/d/VOOY/media')


def required_capacity(media_bytes: int, db_bytes: int) -> int:
    return ((media_bytes + db_bytes) * 11 + 9) // 10 + 2 * 1024 ** 3


def media_size(root: Path) -> int:
    total = 0
    for name in MEDIA_DIRS:
        path = root / name
        if path.is_symlink():
            raise ValueError('미디어 저장소에 심볼릭 링크가 있습니다.')
        if not path.exists():
            continue
        for parent, dirs, files in os.walk(path, followlinks=False):
            for name in dirs + files:
                if (Path(parent) / name).is_symlink():
                    raise ValueError('미디어 저장소에 심볼릭 링크가 있습니다.')
            for name in files:
                total += (Path(parent) / name).stat().st_size
    return total


def write_status(path: Path, status: dict):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(status, ensure_ascii=False), encoding='utf-8')
    temp.chmod(0o600)
    os.replace(temp, path)


def main():
    parser = argparse.ArgumentParser(description='매일 실행하는 Vooy 백업')
    parser.add_argument('--destination', type=Path, default=DEFAULT_DESTINATION)
    parser.add_argument('--media-root', type=Path, default=DEFAULT_MEDIA)
    args = parser.parse_args()
    status_dir = PROJECT_ROOT / '.secrets'
    status_dir.mkdir(mode=0o700, exist_ok=True)
    with (status_dir / 'daily-backup.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('일일 백업이 이미 실행 중입니다.')
            return
        status_file = status_dir / 'backup-status.json'
        try:
            previous = json.loads(status_file.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            previous = {}
        last_success = previous.get('last_success_at')
        status = {'state': 'running', 'started_at': datetime.now(timezone.utc).isoformat(),
                  'destination': str(args.destination), 'last_success_at': last_success}
        write_status(status_dir / 'backup-status.json', status)
        try:
            if args.destination == DEFAULT_DESTINATION and not os.path.ismount('/mnt/c'):
                raise ValueError('C 드라이브가 마운트되지 않았습니다.')
            if args.media_root == DEFAULT_MEDIA and not os.path.ismount('/mnt/d'):
                raise ValueError('D 드라이브가 마운트되지 않았습니다.')
            args.media_root.resolve(strict=True)
            args.destination.mkdir(parents=True, exist_ok=True)
            database_size = subprocess.run(['docker', 'compose', 'exec', '-T', 'postgres', 'psql', '-U', 'vooy',
                                            '-d', 'vooy', '-At', '-c', "SELECT pg_database_size('vooy')"],
                                           cwd=PROJECT_ROOT, check=True, capture_output=True, text=True)
            needed = required_capacity(media_size(args.media_root), int(database_size.stdout.strip()))
            available = shutil.disk_usage(args.destination).free
            status.update(required_bytes=needed, available_bytes=available)
            if available < needed:
                raise ValueError(f'백업 공간 부족: 필요 {needed}바이트, 여유 {available}바이트')
            result = subprocess.run([sys.executable, str(PROJECT_ROOT / 'scripts' / 'backup_storage.py'),
                                     '--destination', str(args.destination), '--media-root', str(args.media_root)],
                                    cwd=PROJECT_ROOT, check=True, capture_output=True, text=True)
            status.update(state='succeeded', message=result.stdout.strip()[-4000:],
                          last_success_at=datetime.now(timezone.utc).isoformat())
            print(result.stdout, end='')
        except (ValueError, OSError, subprocess.CalledProcessError) as error:
            message = (error.stderr if isinstance(error, subprocess.CalledProcessError) and error.stderr else str(error))[-4000:]
            status.update(state='failed', error=message)
            subprocess.run(['logger', '-p', 'user.err', '-t', 'vooy-mcp-backup', message], check=False)
            print(f'일일 백업 실패: {message}', file=sys.stderr)
            raise SystemExit(1)
        finally:
            status['finished_at'] = datetime.now(timezone.utc).isoformat()
            write_status(status_dir / 'backup-status.json', status)


if __name__ == '__main__':
    main()
