"""Automatic scheduling integration; requires an isolated PostgreSQL test database."""
import os
from pathlib import Path
import tempfile
import unittest
import uuid

import psycopg
from psycopg import sql
from vooy_storage.auto_ingestion import AutoIngestion
from vooy_storage.rag import RAGService
from vooy_storage.repository import Repository


@unittest.skipUnless(os.getenv('VOOY_TEST_DATABASE_URL'), 'requires test PostgreSQL')
class AutoIngestionTest(unittest.TestCase):
    def setUp(self):
        self.db = os.environ['VOOY_TEST_DATABASE_URL']
        self.schema = 'autotest_' + uuid.uuid4().hex
        with psycopg.connect(self.db, autocommit=True) as conn:
            conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.schema)))
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.addCleanup(self.drop_schema)
        schema = self.schema
        class TestRepo(Repository):
            def connect(self):
                conn = super().connect()
                conn.execute(sql.SQL('SET search_path TO {}, public').format(sql.Identifier(schema)))
                return conn
        self.repo = TestRepo(self.db, Path(directory.name))
        self.repo.init_schema()
        self.rag = RAGService(self.repo)
        self.rag.init_schema()
        self.space = self.rag.register_space('test', '1', 3, 'cosine', 'unit', 'p1', ['text', 'image'],
                                             document_encoder='doc', query_encoder='query')
        self.scheduler = AutoIngestion(self.repo, self.space['id'])

    def drop_schema(self):
        with psycopg.connect(self.db, autocommit=True) as conn:
            conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema)))

    def test_restart_is_idempotent_and_failed_job_is_not_requeued(self):
        first = self.repo.save_text('추억', '첫 여행')
        self.assertTrue(self.scheduler.run_once())
        self.assertFalse(self.scheduler.run_once())
        restarted = AutoIngestion(self.repo, self.space['id'])
        self.assertFalse(restarted.run_once())
        jobs = self.rag.status_for_asset(first['id'])
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]['pipeline_revision'], 'openrouter-image-v1')
        claimed = self.rag.claim(['openrouter-image-v1'], [self.space['id']])
        self.rag.fail(claimed['id'], claimed['lease_epoch'], 'bad_key', retryable=False)
        self.assertFalse(restarted.run_once())
        second = self.repo.save_text('새 사진 메모', '둘째 여행')
        self.assertTrue(restarted.run_once())
        self.assertEqual(len(self.rag.status_for_asset(second['id'])), 1)

    def test_existing_manual_job_suppresses_auto_job(self):
        asset = self.repo.save_text('장소', '카페')
        self.rag.enqueue(asset['id'], self.space['id'], pipeline_revision='other', request_key='manual')
        self.assertFalse(self.scheduler.run_once())
        self.assertEqual(len(self.rag.status_for_asset(asset['id'])), 1)
