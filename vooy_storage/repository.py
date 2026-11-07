"""PostgreSQL catalogue and immutable local media objects for Vooy MCP."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import uuid

import psycopg
from psycopg.rows import dict_row


class Repository:
    def __init__(self, database_url: str, media_root: str | Path, password: str | None = None):
        self.database_url = database_url
        self.password = password
        self.media_root = Path(media_root).resolve()

    def connect(self):
        options = {"row_factory": dict_row, "connect_timeout": 5}
        if self.password is not None:
            options["password"] = self.password
        return psycopg.connect(self.database_url, **options)

    def init_schema(self):
        self.media_root.mkdir(parents=True, exist_ok=True)
        (self.media_root / "originals").mkdir(exist_ok=True)
        with self.connect() as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            conn.execute("""CREATE TABLE IF NOT EXISTS assets (
                id uuid PRIMARY KEY, version_id uuid NOT NULL UNIQUE,
                title text NOT NULL, kind text NOT NULL,
                filename text, mime_type text NOT NULL,
                size_bytes bigint NOT NULL CHECK (size_bytes >= 0),
                sha256 char(64) NOT NULL, object_key text,
                text_body text, tags text[] NOT NULL DEFAULT '{}', source text,
                created_at timestamptz NOT NULL DEFAULT now(),
                updated_at timestamptz NOT NULL DEFAULT now(), deleted_at timestamptz,
                CHECK ((object_key IS NOT NULL AND text_body IS NULL) OR
                       (object_key IS NULL AND text_body IS NOT NULL))
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS text_requests (
                request_key text PRIMARY KEY, payload_hash char(64) NOT NULL,
                asset_id uuid NOT NULL REFERENCES assets(id)
            )""")
            conn.execute("CREATE INDEX IF NOT EXISTS assets_created_idx ON assets (created_at DESC, id DESC) WHERE deleted_at IS NULL")
            conn.execute("""CREATE TABLE IF NOT EXISTS asset_versions (
                id uuid PRIMARY KEY, asset_id uuid NOT NULL REFERENCES assets(id),
                version_no integer NOT NULL CHECK (version_no > 0),
                parent_version_id uuid,
                kind text NOT NULL, filename text, mime_type text NOT NULL,
                size_bytes bigint NOT NULL CHECK (size_bytes >= 0), sha256 char(64) NOT NULL,
                object_key text, text_body text, created_at timestamptz NOT NULL DEFAULT now(),
                UNIQUE (asset_id, version_no),
                CHECK ((object_key IS NOT NULL AND text_body IS NULL) OR
                       (object_key IS NULL AND text_body IS NOT NULL))
            )""")
            conn.execute("ALTER TABLE asset_versions ADD COLUMN IF NOT EXISTS parent_version_id uuid")
            # Existing v1 rows remain the source of truth during this idempotent migration.
            conn.execute("""INSERT INTO asset_versions
                (id,asset_id,version_no,kind,filename,mime_type,size_bytes,sha256,object_key,text_body,created_at)
                SELECT version_id,id,1,kind,filename,mime_type,size_bytes,sha256,object_key,text_body,created_at
                FROM assets a WHERE NOT EXISTS (SELECT 1 FROM asset_versions v WHERE v.asset_id=a.id)
                ON CONFLICT DO NOTHING""")
            conn.execute("""CREATE TABLE IF NOT EXISTS collections (
                id uuid PRIMARY KEY, name text NOT NULL UNIQUE, description text,
                created_at timestamptz NOT NULL DEFAULT now())""")
            conn.execute("""CREATE TABLE IF NOT EXISTS asset_collections (
                collection_id uuid NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
                asset_id uuid NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
                PRIMARY KEY(collection_id, asset_id))""")
            conn.execute("""CREATE TABLE IF NOT EXISTS cleanup_jobs (
                asset_id uuid PRIMARY KEY REFERENCES assets(id), state text NOT NULL DEFAULT 'queued',
                attempt integer NOT NULL DEFAULT 0, lease_token uuid, lease_until timestamptz,
                next_run_at timestamptz NOT NULL DEFAULT now(), last_error text,
                created_at timestamptz NOT NULL DEFAULT now(), completed_at timestamptz)""")

    @staticmethod
    def _record_version(conn, row, version_no):
        conn.execute("""INSERT INTO asset_versions
            (id,asset_id,version_no,kind,filename,mime_type,size_bytes,sha256,object_key,text_body)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (row['version_id'], row['id'], version_no, row['kind'], row['filename'],
             row['mime_type'], row['size_bytes'], row['sha256'], row['object_key'], row['text_body']))

    @staticmethod
    def _asset(row):
        if row is None:
            return None
        item = dict(row)
        item["id"] = str(item["id"])
        item["version_id"] = str(item["version_id"])
        item["text"] = item.pop("text_body")
        for name in ("created_at", "updated_at"):
            item[name] = item[name].isoformat()
        item.pop("deleted_at", None)
        return item

    def save_text(self, title: str, text: str, tags=None, source=None, request_key=None):
        if not title or not isinstance(title, str):
            raise ValueError("제목을 입력해 주세요.")
        if not isinstance(text, str):
            raise ValueError("텍스트를 입력해 주세요.")
        data = text.encode("utf-8")
        if len(data) > 64 * 1024:
            raise ValueError("텍스트는 UTF-8 기준 64 KiB 이하여야 합니다.")
        tags = _normalize_tags(tags)
        payload = json.dumps([title, text, tags, source], ensure_ascii=False, separators=(",", ":"))
        payload_hash = hashlib.sha256(payload.encode()).hexdigest()
        asset_id, version_id = uuid.uuid4(), uuid.uuid4()
        with self.connect() as conn:
            if request_key:
                # Serialize identical keys, including the first insert.
                conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (request_key,))
                existing = conn.execute("SELECT payload_hash, asset_id FROM text_requests WHERE request_key = %s", (request_key,)).fetchone()
                if existing:
                    if existing["payload_hash"] != payload_hash:
                        raise ValueError("요청 키가 다른 내용에 이미 사용되었습니다.")
                    row = conn.execute("SELECT * FROM assets WHERE id = %s AND deleted_at IS NULL", (existing["asset_id"],)).fetchone()
                    if row is None:
                        raise ValueError("삭제된 요청 키는 다시 사용할 수 없습니다.")
                    return self._asset(row)
            row = conn.execute("""INSERT INTO assets
                (id, version_id, title, kind, mime_type, size_bytes, sha256, text_body, tags, source)
                VALUES (%s,%s,%s,'text','text/plain; charset=utf-8',%s,%s,%s,%s,%s) RETURNING *""",
                (asset_id, version_id, title, len(data), hashlib.sha256(data).hexdigest(), text, tags, source)).fetchone()
            self._record_version(conn, row, 1)
            if request_key:
                conn.execute("INSERT INTO text_requests (request_key, payload_hash, asset_id) VALUES (%s,%s,%s)", (request_key, payload_hash, asset_id))
            return self._asset(row)

    def save_file(self, title, filename, media_type, staged_path, size, sha256, asset_id=None,
                  tags=None, source=None):
        if not title or not filename:
            raise ValueError("제목과 파일 이름이 필요합니다.")
        if size < 0 or size > 10 * 1024**3:
            raise ValueError("파일 크기가 허용 범위를 벗어났습니다.")
        tags = _normalize_tags(tags)
        if source is not None and not isinstance(source, str):
            raise ValueError("출처는 문자열이어야 합니다.")
        media_type = media_type.lower().split(";", 1)[0].strip()
        allowed = {"image/jpeg", "image/png", "image/webp", "image/gif", "image/heic", "video/mp4", "video/quicktime", "video/webm", "audio/mpeg", "audio/wav", "audio/x-wav", "audio/mp4", "application/pdf", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "text/plain", "text/markdown", "application/json"}
        if media_type not in allowed:
            raise ValueError("지원하지 않는 파일 형식입니다.")
        kind = ("image" if media_type.startswith("image/") else "video" if media_type.startswith("video/") else "audio" if media_type.startswith("audio/") else "document")
        asset_id = uuid.UUID(str(asset_id)) if asset_id is not None else uuid.uuid4()
        version_id = uuid.uuid5(asset_id, "original-version-1")
        key = f"originals/{asset_id}/{version_id}/source"
        destination = self.media_root / key
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (str(asset_id),))
            existing = conn.execute("SELECT * FROM assets WHERE id = %s", (asset_id,)).fetchone()
            if existing:
                if existing["deleted_at"] is not None:
                    raise ValueError("삭제된 자료는 다시 저장할 수 없습니다.")
                if existing["sha256"] != sha256.lower() or existing["size_bytes"] != size:
                    raise ValueError("동일한 자료 ID에 다른 파일이 있습니다.")
                return self._asset(existing)
            fd, temporary = tempfile.mkstemp(prefix=".copy-", dir=destination.parent)
            actual_hash = hashlib.sha256()
            actual_size = 0
            try:
                with os.fdopen(fd, "wb") as out, open(staged_path, "rb") as src:
                    while chunk := src.read(1024 * 1024):
                        actual_size += len(chunk)
                        if actual_size > size:
                            raise ValueError("파일 크기가 예상과 다릅니다.")
                        actual_hash.update(chunk)
                        out.write(chunk)
                    out.flush()
                    os.fsync(out.fileno())
                if actual_size != size or actual_hash.hexdigest() != sha256.lower():
                    raise ValueError("파일 크기 또는 SHA-256이 일치하지 않습니다.")
                os.replace(temporary, destination)
                row = conn.execute("""INSERT INTO assets
                    (id, version_id, title, kind, filename, mime_type, size_bytes, sha256, object_key, tags, source)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                    (asset_id, version_id, title, kind, Path(filename.replace("\\", "/")).name, media_type, size, sha256.lower(), key, tags, source)).fetchone()
                self._record_version(conn, row, 1)
                return self._asset(row)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)

    def get_asset(self, asset_id, version_id=None):
        try:
            asset_id = uuid.UUID(str(asset_id))
        except ValueError:
            return None
        with self.connect() as conn:
            asset = self._asset(conn.execute("SELECT * FROM assets WHERE id = %s AND deleted_at IS NULL", (asset_id,)).fetchone())
            if not asset or version_id is None:
                return asset
            try:
                version_id = uuid.UUID(str(version_id))
            except (ValueError, TypeError):
                return None
            version = conn.execute("SELECT * FROM asset_versions WHERE id=%s AND asset_id=%s", (version_id, asset_id)).fetchone()
            if version is None:
                return None
            for key in ('kind', 'filename', 'mime_type', 'size_bytes', 'sha256', 'object_key'):
                asset[key] = version[key]
            asset['version_id'] = str(version['id'])
            asset['version_no'] = version['version_no']
            asset['text'] = version['text_body']
            return asset

    def list_assets(self, query=None, kind=None, tags=None, limit=20, cursor=None, collection_id=None):
        if not 1 <= limit <= 100:
            raise ValueError("목록 개수는 1~100이어야 합니다.")
        clauses = ["deleted_at IS NULL"]
        values = []
        if query:
            clauses.append("(title ILIKE %s OR COALESCE(text_body, '') ILIKE %s OR EXISTS (SELECT 1 FROM unnest(tags) t WHERE t ILIKE %s))")
            pattern = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            values.extend([pattern] * 3)
        if kind:
            clauses.append("kind = %s")
            values.append(kind)
        if tags:
            clauses.append("tags @> %s::text[]")
            values.append(_normalize_tags(tags))
        if collection_id:
            try:
                collection_id = uuid.UUID(str(collection_id))
            except (ValueError, TypeError) as exc:
                raise ValueError("컬렉션 ID가 올바르지 않습니다.") from exc
            clauses.append("EXISTS (SELECT 1 FROM asset_collections ac WHERE ac.asset_id=assets.id AND ac.collection_id=%s)")
            values.append(collection_id)
        if cursor:
            try:
                cursor_id = uuid.UUID(str(cursor))
            except ValueError as exc:
                raise ValueError("목록 커서가 올바르지 않습니다.") from exc
            clauses.append("(created_at,id) < (SELECT created_at,id FROM assets WHERE id = %s)")
            values.append(cursor_id)
        values.append(limit + 1)
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM assets WHERE " + " AND ".join(clauses) + " ORDER BY created_at DESC, id DESC LIMIT %s", values).fetchall()
        items = [self._asset(row) for row in rows[:limit]]
        for item in items:
            if item["text"] is not None and len(item["text"]) > 300:
                item["text"] = item["text"][:300]
        return {"assets": items, "next_cursor": items[-1]["id"] if len(rows) > limit else None}

    def asset_path(self, asset):
        key = asset.get("object_key")
        if not key:
            raise ValueError("파일 원본이 없는 자료입니다.")
        path = (self.media_root / key).resolve()
        if not path.is_relative_to(self.media_root / "originals"):
            raise ValueError("저장 경로가 올바르지 않습니다.")
        return path

    def update_asset(self, asset_id, title=None, tags=None, source=None):
        current = self.get_asset(asset_id)
        if not current:
            return None
        if title is not None and not title:
            raise ValueError("제목을 입력해 주세요.")
        with self.connect() as conn:
            row = conn.execute("""UPDATE assets SET title = %s, tags = %s, source = %s, updated_at = now()
                WHERE id = %s AND deleted_at IS NULL RETURNING *""", (
                title if title is not None else current["title"],
                _normalize_tags(tags) if tags is not None else current["tags"],
                source if source is not None else current["source"], current["id"])).fetchone()
            return self._asset(row)

    def list_versions(self, asset_id):
        try:
            asset_id = uuid.UUID(str(asset_id))
        except (TypeError, ValueError):
            return []
        with self.connect() as conn:
            rows = conn.execute("""SELECT v.id, v.version_no, v.kind, v.filename, v.mime_type,
                v.size_bytes, v.sha256, v.created_at FROM asset_versions v
                JOIN assets a ON a.id=v.asset_id AND a.deleted_at IS NULL
                WHERE v.asset_id=%s ORDER BY v.version_no DESC""", (asset_id,)).fetchall()
        return [{**dict(row), 'id': str(row['id']), 'created_at': row['created_at'].isoformat()} for row in rows]

    def replace_file(self, asset_id, expected_version_id, filename, media_type, staged_path, size, sha256, request_id):
        """Create an immutable file version, serialized with current version changes."""
        try:
            asset_id = uuid.UUID(str(asset_id))
            expected_version_id = uuid.UUID(str(expected_version_id))
        except (ValueError, TypeError) as exc:
            raise ValueError('자료 또는 버전 ID가 올바르지 않습니다.') from exc
        if not request_id or len(str(request_id)) > 200:
            raise ValueError('업로드 요청 ID가 필요합니다.')
        if not filename or not isinstance(filename, str) or size < 0 or size > 10 * 1024**3:
            raise ValueError('파일 이름 또는 크기를 확인해 주세요.')
        media_type = media_type.lower().split(';', 1)[0].strip()
        allowed = {'image/jpeg', 'image/png', 'image/webp', 'image/gif', 'image/heic',
                   'video/mp4', 'video/quicktime', 'video/webm', 'audio/mpeg', 'audio/wav',
                   'audio/x-wav', 'audio/mp4', 'application/pdf', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', 'text/plain', 'text/markdown', 'application/json'}
        if media_type not in allowed:
            raise ValueError('지원하지 않는 파일 형식입니다.')
        kind = ('image' if media_type.startswith('image/') else 'video' if media_type.startswith('video/') else
                'audio' if media_type.startswith('audio/') else 'document')
        version_id = uuid.uuid5(asset_id, 'replace:' + str(request_id))
        key = f'originals/{asset_id}/{version_id}/source'
        destination = self.media_root / key
        destination.parent.mkdir(parents=True, exist_ok=True)
        created_file = False
        try:
            with self.connect() as conn:
                current = conn.execute('SELECT * FROM assets WHERE id=%s AND deleted_at IS NULL FOR UPDATE', (asset_id,)).fetchone()
                if current is None:
                    raise ValueError('자료를 찾을 수 없습니다.')
                prior = conn.execute('SELECT * FROM asset_versions WHERE id=%s AND asset_id=%s', (version_id, asset_id)).fetchone()
                if prior:
                    if (prior['parent_version_id'] != expected_version_id or prior['sha256'] != sha256.lower()
                            or prior['size_bytes'] != size or prior['mime_type'] != media_type):
                        raise ValueError('동일한 요청 ID에 다른 파일이 있습니다.')
                    item = self._asset(current)
                    for field in ('kind', 'filename', 'mime_type', 'size_bytes', 'sha256', 'object_key'):
                        item[field] = prior[field]
                    item['version_id'], item['version_no'], item['text'] = str(version_id), prior['version_no'], prior['text_body']
                    return item
                if current['version_id'] != expected_version_id:
                    raise ValueError('원본 버전이 변경되었습니다. 최신 버전을 확인해 주세요.')
                version_no = conn.execute('SELECT COALESCE(MAX(version_no),0)+1 AS n FROM asset_versions WHERE asset_id=%s', (asset_id,)).fetchone()['n']
                fd, temp = tempfile.mkstemp(prefix='.copy-', dir=destination.parent)
                try:
                    digest, length = hashlib.sha256(), 0
                    with os.fdopen(fd, 'wb') as out, open(staged_path, 'rb') as src:
                        while chunk := src.read(1024 * 1024):
                            length += len(chunk)
                            if length > size:
                                raise ValueError('파일 크기가 예상과 다릅니다.')
                            digest.update(chunk)
                            out.write(chunk)
                        out.flush()
                        os.fsync(out.fileno())
                    if length != size or digest.hexdigest() != sha256.lower():
                        raise ValueError('파일 크기 또는 SHA-256이 일치하지 않습니다.')
                    os.replace(temp, destination)
                    created_file = True
                finally:
                    if os.path.exists(temp):
                        os.unlink(temp)
                row = conn.execute("""UPDATE assets SET version_id=%s,kind=%s,filename=%s,mime_type=%s,
                    size_bytes=%s,sha256=%s,object_key=%s,text_body=NULL,updated_at=now()
                    WHERE id=%s RETURNING *""", (version_id, kind, Path(filename.replace('\\', '/')).name,
                    media_type, size, sha256.lower(), key, asset_id)).fetchone()
                self._record_version(conn, row, version_no)
                conn.execute('UPDATE asset_versions SET parent_version_id=%s WHERE id=%s', (expected_version_id, version_id))
                item = self._asset(row)
                item['version_no'] = version_no
                return item
        except Exception:
            if created_file:
                destination.unlink(missing_ok=True)
            raise

    def create_collection(self, name, description=None):
        if not isinstance(name, str) or not name.strip() or len(name) > 200:
            raise ValueError('컬렉션 이름을 입력해 주세요.')
        with self.connect() as conn:
            row = conn.execute('INSERT INTO collections (id,name,description) VALUES (%s,%s,%s) RETURNING *',
                               (uuid.uuid4(), name.strip(), description)).fetchone()
        return {**dict(row), 'id': str(row['id']), 'created_at': row['created_at'].isoformat()}

    def list_collections(self):
        with self.connect() as conn:
            rows = conn.execute('SELECT * FROM collections ORDER BY created_at DESC,id DESC').fetchall()
        return [{**dict(row), 'id': str(row['id']), 'created_at': row['created_at'].isoformat()} for row in rows]

    def add_to_collection(self, collection_id, asset_id):
        try:
            collection_id, asset_id = uuid.UUID(str(collection_id)), uuid.UUID(str(asset_id))
        except (TypeError, ValueError) as exc:
            raise ValueError('자료 또는 컬렉션 ID가 올바르지 않습니다.') from exc
        with self.connect() as conn:
            row = conn.execute("""INSERT INTO asset_collections (collection_id,asset_id)
                SELECT c.id,a.id FROM collections c CROSS JOIN assets a
                WHERE c.id=%s AND a.id=%s AND a.deleted_at IS NULL
                ON CONFLICT DO NOTHING RETURNING asset_id""", (collection_id, asset_id)).fetchone()
            if row:
                return True
            return bool(conn.execute("""SELECT 1 FROM asset_collections ac JOIN assets a ON a.id=ac.asset_id
                WHERE ac.collection_id=%s AND ac.asset_id=%s AND a.deleted_at IS NULL""", (collection_id, asset_id)).fetchone())

    def remove_from_collection(self, collection_id, asset_id):
        try:
            collection_id, asset_id = uuid.UUID(str(collection_id)), uuid.UUID(str(asset_id))
        except (TypeError, ValueError) as exc:
            raise ValueError('자료 또는 컬렉션 ID가 올바르지 않습니다.') from exc
        with self.connect() as conn:
            return bool(conn.execute('DELETE FROM asset_collections WHERE collection_id=%s AND asset_id=%s RETURNING asset_id',
                                     (collection_id, asset_id)).fetchone())

    def delete_asset(self, asset_id):
        try:
            asset_id = uuid.UUID(str(asset_id))
        except ValueError:
            return False
        with self.connect() as conn:
            row = conn.execute("UPDATE assets SET deleted_at = now() WHERE id = %s AND deleted_at IS NULL RETURNING id", (asset_id,)).fetchone()
            if row:
                conn.execute("INSERT INTO cleanup_jobs (asset_id) VALUES (%s) ON CONFLICT DO NOTHING", (asset_id,))
                if conn.execute("SELECT to_regclass('rag_jobs') AS table_name").fetchone()['table_name']:
                    conn.execute("""UPDATE rag_jobs SET state='canceled', lease_epoch=lease_epoch+1,
                        lease_until=NULL,updated_at=now() WHERE asset_id=%s AND state IN ('queued','running')""", (asset_id,))
            return row is not None


def _normalize_tags(tags):
    if tags is None:
        return []
    if not isinstance(tags, list) or any(not isinstance(t, str) or len(t) > 100 for t in tags):
        raise ValueError("태그는 100자 이내 문자열 목록이어야 합니다.")
    return list(dict.fromkeys(t.strip() for t in tags if t.strip()))
