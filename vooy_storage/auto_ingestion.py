"""Durably schedule eligible saved asset versions for a configured RAG space."""
import logging
import threading
import uuid

LOG = logging.getLogger(__name__)


class AutoIngestion:
    def __init__(self, repository, space_id, pipeline_revision='openrouter-image-v1', poll_seconds=5):
        self.repository = repository
        self.space_id = uuid.UUID(str(space_id))
        self.pipeline_revision = pipeline_revision
        self.poll_seconds = poll_seconds
        self.stop_event = threading.Event()
        self.thread = None

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._loop, name='auto-ingestion', daemon=True)
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
                LOG.exception('Automatic ingestion scheduling failed')
            self.stop_event.wait(self.poll_seconds)

    def run_once(self):
        # Asset row lock is shared with version replacement, deletion and manual enqueue.
        # Existing jobs suppress re-enqueue, including failed jobs requiring manual retry.
        with self.repository.connect() as conn:
            asset = conn.execute('''SELECT a.id, a.version_id FROM assets a
                JOIN rag_spaces s ON s.id = %s
                WHERE a.deleted_at IS NULL AND a.kind IN ('image', 'text', 'video', 'audio', 'document')
                  AND s.modalities @> jsonb_build_array(a.kind)
                  AND NOT EXISTS (SELECT 1 FROM rag_jobs j
                      WHERE j.asset_id=a.id AND j.asset_version_id=a.version_id
                      AND j.space_id=s.id AND j.state <> 'superseded')
                  AND NOT EXISTS (SELECT 1 FROM rag_jobs j
                      WHERE j.asset_id=a.id AND j.space_id=s.id
                      AND j.request_key='auto-' || a.version_id::text)
                  AND NOT EXISTS (SELECT 1 FROM rag_result_sets r
                      WHERE r.asset_id=a.id AND r.asset_version_id=a.version_id
                      AND r.space_id=s.id AND r.state='active')
                ORDER BY a.created_at, a.id FOR UPDATE OF a SKIP LOCKED LIMIT 1''',
                (self.space_id,)).fetchone()
            if not asset:
                return False
            key = 'auto-' + str(asset['version_id'])
            row = conn.execute('''INSERT INTO rag_jobs
                (id,asset_id,asset_version_id,space_id,pipeline_revision,request_key,state)
                VALUES (%s,%s,%s,%s,%s,%s,'queued')
                ON CONFLICT (asset_id,space_id,request_key) DO NOTHING RETURNING id''',
                (uuid.uuid4(), asset['id'], asset['version_id'], self.space_id, self.pipeline_revision, key)).fetchone()
            return row is not None
