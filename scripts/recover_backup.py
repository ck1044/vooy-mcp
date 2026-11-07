"""Restore service state after a stopped or interrupted scheduled backup."""
from datetime import datetime, timezone
import fcntl
import json
from pathlib import Path
import subprocess
import sys

from backup_storage import PROJECT_ROOT, compose
from daily_backup import write_status


def main():
    state_dir = PROJECT_ROOT / '.secrets'
    state_dir.mkdir(mode=0o700, exist_ok=True)
    with (state_dir / 'backup.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        marker = state_dir / 'backup-maintenance.json'
        if marker.exists():
            services = json.loads(marker.read_text(encoding='utf-8'))['services']
            if services:
                compose('start', *services)
            marker.unlink()
    status_file = state_dir / 'backup-status.json'
    if status_file.exists():
        status = json.loads(status_file.read_text(encoding='utf-8'))
        if status.get('state') == 'running':
            status.update(state='failed', error='백업이 중단되어 서비스를 복구했습니다.',
                          finished_at=datetime.now(timezone.utc).isoformat())
            write_status(status_file, status)
            subprocess.run(['logger', '-p', 'user.err', '-t', 'vooy-mcp-backup', status['error']], check=False)


if __name__ == '__main__':
    try:
        main()
    except (OSError, KeyError, ValueError, subprocess.CalledProcessError) as error:
        print(f'백업 후 서비스 복구 실패: {error}', file=sys.stderr)
        raise SystemExit(1)
