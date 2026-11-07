"""Real PostgreSQL integration checks, isolated in a temporary schema."""
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
import uuid

try:
    import psycopg
    from psycopg import sql
    from vooy_storage.repository import Repository
except ImportError:
    psycopg = None


@unittest.skipUnless(psycopg and os.getenv("VOOY_TEST_DATABASE_URL"), "VOOY_TEST_DATABASE_URL and psycopg are required")
class RepositoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.database_url = os.environ["VOOY_TEST_DATABASE_URL"]
        cls.schema = "test_" + uuid.uuid4().hex
        with psycopg.connect(cls.database_url, autocommit=True) as conn:
            conn.execute(sql.SQL("CREATE SCHEMA {} ").format(sql.Identifier(cls.schema)))

    @classmethod
    def tearDownClass(cls):
        with psycopg.connect(cls.database_url, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(cls.schema)))

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        schema = self.schema
        class TestRepository(Repository):
            def connect(self):
                conn = super().connect()
                conn.execute(sql.SQL("SET search_path TO {} ").format(sql.Identifier(schema)))
                return conn
        self.repo = TestRepository(self.database_url, self.directory.name)
        self.repo.init_schema()

    def test_text_idempotency_and_validation(self):
        first = self.repo.save_text("제목", "한글 메모", tags=["추억", "추억"], request_key="text-" + uuid.uuid4().hex)
        self.assertEqual(self.repo.get_asset(first["id"])["text"], "한글 메모")
        self.assertEqual(first["tags"], ["추억"])
        with self.assertRaises(ValueError):
            self.repo.save_text("large", "가" * 22000)
        key = "retry-" + uuid.uuid4().hex
        saved = self.repo.save_text("제목", "다시 시도", request_key=key)
        self.assertEqual(saved["id"], self.repo.save_text("제목", "다시 시도", request_key=key)["id"])
        with self.assertRaises(ValueError):
            self.repo.save_text("제목", "다른 텍스트", request_key=key)

    def test_file_retry_checksum_and_delete(self):
        data = b"sample image bytes" * 100
        stage = Path(self.directory.name) / "upload"
        stage.write_bytes(data)
        sha = hashlib.sha256(data).hexdigest()
        item_id = str(uuid.uuid4())
        asset = self.repo.save_file("사진", "../photo.jpg", "image/jpeg", stage, len(data), sha, asset_id=item_id)
        self.assertEqual(asset["filename"], "photo.jpg")
        self.assertEqual(self.repo.asset_path(asset).read_bytes(), data)
        again = self.repo.save_file("사진", "photo.jpg", "image/jpeg", stage, len(data), sha, asset_id=item_id)
        self.assertEqual(again["id"], asset["id"])
        with self.assertRaises(ValueError):
            self.repo.save_file("사진", "photo.jpg", "image/jpeg", stage, len(data), "a" * 64, asset_id=item_id)
        self.assertTrue(self.repo.delete_asset(item_id))
        self.assertIsNone(self.repo.get_asset(item_id))
        with self.assertRaises(ValueError):
            self.repo.save_file("사진", "photo.jpg", "image/jpeg", stage, len(data), sha, asset_id=item_id)

    def test_pagination_and_path_isolation(self):
        tag = uuid.uuid4().hex
        for n in range(3):
            self.repo.save_text("자료", str(n), tags=[tag])
        first = self.repo.list_assets(tags=[tag], limit=2)
        second = self.repo.list_assets(tags=[tag], limit=2, cursor=first["next_cursor"])
        self.assertEqual((len(first["assets"]), len(second["assets"])), (2, 1))
        self.assertIsNone(second["next_cursor"])
        with self.assertRaises(ValueError):
            self.repo.asset_path({"object_key": "../../escape"})

if __name__ == "__main__":
    unittest.main()
