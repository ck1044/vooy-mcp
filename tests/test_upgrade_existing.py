"""Upgrade a v1 database while preserving existing assets."""
import os
from pathlib import Path
import tempfile
import unittest
import uuid

import psycopg
from psycopg import sql
from vooy_storage.repository import Repository


@unittest.skipUnless(os.getenv('VOOY_TEST_DATABASE_URL'), 'temporary PostgreSQL required')
class UpgradeTests(unittest.TestCase):
    def test_backfills_existing_asset_version(self):
        url = os.environ['VOOY_TEST_DATABASE_URL']
        schema = 'upgrade_' + uuid.uuid4().hex
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        self.addCleanup(self.drop_schema, url, schema)
        with tempfile.TemporaryDirectory() as media:
            class ScopedRepository(Repository):
                def connect(self):
                    conn = super().connect()
                    conn.execute(sql.SQL('SET search_path TO {}, public').format(sql.Identifier(schema)))
                    return conn
            repo = ScopedRepository(url, Path(media))
            with repo.connect() as conn:
                conn.execute('''CREATE TABLE assets (id uuid PRIMARY KEY, version_id uuid NOT NULL UNIQUE,
                    title text NOT NULL, kind text NOT NULL, filename text, mime_type text NOT NULL,
                    size_bytes bigint NOT NULL, sha256 char(64) NOT NULL, object_key text, text_body text,
                    tags text[] NOT NULL DEFAULT '{}', source text, created_at timestamptz NOT NULL DEFAULT now(),
                    updated_at timestamptz NOT NULL DEFAULT now(), deleted_at timestamptz)''')
                asset, version = uuid.uuid4(), uuid.uuid4()
                conn.execute("INSERT INTO assets (id,version_id,title,kind,mime_type,size_bytes,sha256,text_body) VALUES (%s,%s,'기존','text','text/plain',3,%s,'old')", (asset,version,'a'*64))
            repo.init_schema()
            repo.init_schema()
            versions = repo.list_versions(str(asset))
            self.assertEqual(len(versions), 1)
            self.assertEqual(versions[0]['id'], str(version))
            self.assertEqual(repo.get_asset(str(asset))['text'], 'old')

    @staticmethod
    def drop_schema(url, schema):
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
