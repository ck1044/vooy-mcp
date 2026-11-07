"""Durable cleanup for assets deleted after this feature is enabled."""
from __future__ import annotations

import logging
from pathlib import Path
import shutil
import threading
import uuid


logger = logging.getLogger(__name__)


class CleanupManager:
    def __init__(self, repository, poll_seconds=5):
        self.repository = repository
        self.poll_seconds = poll_seconds
        self.stop_event = threading.Event()
        self.thread = None

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._loop, name='asset-cleanup', daemon=True)
            self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=10)

    def _loop(self):
        while not self.stop_event.is_set():
            try:
                if self.run_once():
                    continue
            except Exception:
                logger.exception('Asset cleanup polling failed')
            self.stop_event.wait(self.poll_seconds)

    def _remove_tree(self, category, *ids):
        root = self.repository.media_root.resolve()
        current = root / category
        for part in ids:
            current = current / str(uuid.UUID(str(part)))
        # Refuse directory redirection outside the media store; unlink symlinks only.
        if not current.parent.resolve().is_relative_to(root):
            raise ValueError('cleanup path leaves media root')
        if current.is_symlink():
            current.unlink()
        elif current.exists():
            if not current.is_dir():
                raise ValueError('cleanup target is not a directory')
            shutil.rmtree(current)

    def run_once(self):
        token = uuid.uuid4()
        with self.repository.connect() as conn:
            job = conn.execute("""SELECT q.asset_id FROM cleanup_jobs q JOIN assets a ON a.id=q.asset_id
                WHERE a.deleted_at IS NOT NULL AND ((q.state='queued' AND q.next_run_at<=now())
                  OR (q.state='running' AND q.lease_until<now()))
                ORDER BY q.created_at FOR UPDATE OF q SKIP LOCKED LIMIT 1""").fetchone()
            if not job:
                return False
            asset_id = job['asset_id']
            conn.execute("""UPDATE cleanup_jobs SET state='running',attempt=attempt+1,lease_token=%s,
                lease_until=now()+interval '15 minutes',last_error=NULL WHERE asset_id=%s""", (token,asset_id))
            versions = [row['id'] for row in conn.execute('SELECT id FROM asset_versions WHERE asset_id=%s', (asset_id,)).fetchall()]
            rag_present = bool(conn.execute("SELECT to_regclass('rag_jobs') AS n").fetchone()['n'])
            artifact_ids = []
            if rag_present:
                artifact_ids = [row['id'] for row in conn.execute("""SELECT t.id FROM rag_artifacts t
                    JOIN rag_result_sets r ON r.id=t.result_set_id WHERE r.asset_id=%s""", (asset_id,)).fetchall()]
        try:
            self._remove_tree('originals', asset_id)
            for version in versions:
                self._remove_tree('derived', version)
            for artifact_id in artifact_ids:
                self._remove_tree('staging/rag', artifact_id)
            with self.repository.connect() as conn:
                job = conn.execute("""SELECT q.asset_id FROM cleanup_jobs q JOIN assets a ON a.id=q.asset_id
                    WHERE q.asset_id=%s AND q.state='running' AND q.lease_token=%s AND a.deleted_at IS NOT NULL
                    FOR UPDATE OF q,a""", (asset_id,token)).fetchone()
                if not job:
                    return True
                if conn.execute("SELECT to_regclass('oba_import_metadata') AS name").fetchone()['name']:
                    conn.execute('DELETE FROM oba_import_metadata WHERE asset_id=%s', (asset_id,))
                if rag_present:
                    if conn.execute("SELECT to_regclass('image_metadata') AS name").fetchone()['name']:
                        conn.execute('DELETE FROM image_metadata WHERE asset_version_id IN (SELECT id FROM asset_versions WHERE asset_id=%s)', (asset_id,))
                    conn.execute("""DELETE FROM rag_artifact_parts WHERE artifact_id IN
                        (SELECT t.id FROM rag_artifacts t JOIN rag_result_sets r ON r.id=t.result_set_id WHERE r.asset_id=%s)""", (asset_id,))
                    for table in ('rag_artifacts','rag_embeddings','rag_batches','rag_segments'):
                        conn.execute(f'DELETE FROM {table} WHERE result_set_id IN (SELECT id FROM rag_result_sets WHERE asset_id=%s)', (asset_id,))
                    conn.execute('DELETE FROM rag_result_sets WHERE asset_id=%s', (asset_id,))
                    conn.execute('DELETE FROM rag_jobs WHERE asset_id=%s', (asset_id,))
                conn.execute("""UPDATE cleanup_jobs SET state='completed',completed_at=now(),
                    lease_token=NULL,lease_until=NULL WHERE asset_id=%s AND lease_token=%s""", (asset_id,token))
        except Exception as exc:
            logger.warning('Cleanup failed; retry scheduled for asset %s: %s', asset_id, type(exc).__name__)
            with self.repository.connect() as conn:
                conn.execute("""UPDATE cleanup_jobs SET state='queued',next_run_at=now()+interval '30 seconds',
                    lease_token=NULL,lease_until=NULL,last_error=%s WHERE asset_id=%s AND lease_token=%s""",
                    (type(exc).__name__,asset_id,token))
        return True
