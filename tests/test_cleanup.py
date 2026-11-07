"""Deletion queues only new requests and safely removes files and RAG results."""
from pathlib import Path
from test_rag_integration import RagIntegration
from vooy_storage.cleanup import CleanupManager


class CleanupIntegration(RagIntegration):
    def test_new_delete_excludes_search_and_removes_rag(self):
        job = self.job()
        result = self.rag.create_result(job['id'], job['lease_epoch'])
        self.batch(result, job)
        self.rag.complete(result['id'], job['lease_epoch'], {'batches':['batch-1'],'segment_count':1,'vector_count':1})
        original = self.repo.media_root / 'originals' / self.asset['id'] / 'example'
        original.parent.mkdir(parents=True)
        original.write_bytes(b'original')
        derived = self.repo.media_root / 'derived' / self.asset['version_id'] / result['id'] / 'example'
        derived.parent.mkdir(parents=True)
        derived.write_bytes(b'derived')
        self.repo.delete_asset(self.asset['id'])
        self.assertEqual(self.rag.search_vector(self.space['id'], [1,0,0])['results'], [])
        self.assertTrue(CleanupManager(self.repo).run_once())
        self.assertFalse(original.exists())
        self.assertFalse(derived.exists())
        with self.repo.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) n FROM rag_embeddings').fetchone()['n'], 0)
            self.assertEqual(conn.execute('SELECT state FROM cleanup_jobs WHERE asset_id=%s', (self.asset['id'],)).fetchone()['state'], 'completed')

    def test_previously_deleted_records_are_not_queued(self):
        with self.repo.connect() as conn:
            conn.execute('UPDATE assets SET deleted_at=now() WHERE id=%s', (self.asset['id'],))
        self.repo.init_schema()
        self.assertFalse(CleanupManager(self.repo).run_once())

    def test_failure_is_retried_and_expired_lease_is_reclaimed(self):
        self.repo.delete_asset(self.asset['id'])
        manager = CleanupManager(self.repo)
        remove = manager._remove_tree
        manager._remove_tree = lambda *_args: (_ for _ in ()).throw(OSError('transient'))
        self.assertTrue(manager.run_once())
        with self.repo.connect() as conn:
            row = conn.execute('SELECT state,attempt FROM cleanup_jobs WHERE asset_id=%s',(self.asset['id'],)).fetchone()
            self.assertEqual((row['state'],row['attempt']),('queued',1))
            conn.execute("UPDATE cleanup_jobs SET state='running',lease_until=now()-interval '1 second' WHERE asset_id=%s",(self.asset['id'],))
        manager._remove_tree = remove
        self.assertTrue(manager.run_once())
        with self.repo.connect() as conn:
            row = conn.execute('SELECT state,attempt FROM cleanup_jobs WHERE asset_id=%s',(self.asset['id'],)).fetchone()
            self.assertEqual((row['state'],row['attempt']),('completed',2))
