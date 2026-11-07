"""Durable RAG job leases and atomic pgvector result sets."""
from __future__ import annotations

from datetime import date, datetime
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import shutil
import tempfile
import uuid

from psycopg.types.json import Jsonb
from psycopg.errors import UniqueViolation
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.routing import Route


class Conflict(ValueError):
    """The requested state transition is no longer valid."""


def _uuid(value):
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError('올바른 ID가 아닙니다.') from exc


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def validate_vectors(vectors, space):
    if not isinstance(vectors, list) or not vectors or len(vectors) > 256:
        raise ValueError('벡터 개수가 올바르지 않습니다.')
    if space['representation'] == 'single_vector' and len(vectors) != 1:
        raise ValueError('단일 벡터 공간에는 벡터 하나가 필요합니다.')
    output = []
    for ordinal, entry in enumerate(vectors):
        if not isinstance(entry, dict) or entry.get('ordinal') != ordinal:
            raise ValueError('벡터 순번이 올바르지 않습니다.')
        values = entry.get('values')
        if not isinstance(values, list) or len(values) != space['dimensions']:
            raise ValueError('임베딩 차원이 공간과 일치하지 않습니다.')
        if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) or abs(x) > 3.4028235e38 for x in values):
            raise ValueError('벡터는 유한한 숫자여야 합니다.')
        vals = [float(x) for x in values]
        norm = math.sqrt(sum(x*x for x in vals))
        if not math.isfinite(norm) or (space['metric'] == 'cosine' and norm == 0):
            raise ValueError('벡터 크기가 올바르지 않습니다.')
        if space['normalization'] == 'unit' and abs(norm - 1) > 0.001:
            raise ValueError('정규화된 단위 벡터가 아닙니다.')
        output.append(vals)
    return output


def _vector(values):
    return '[' + ','.join(str(v) for v in values) + ']'


def _row(value):
    if value is None:
        return None
    result = dict(value)
    for key, val in result.items():
        if isinstance(val, uuid.UUID):
            result[key] = str(val)
        elif hasattr(val, 'isoformat'):
            result[key] = val.isoformat()
    return result


class RAGService:
    def __init__(self, repository):
        self.repository = repository

    def init_schema(self):
        with self.repository.connect() as conn:
            conn.execute('CREATE EXTENSION IF NOT EXISTS vector')
            conn.execute('''CREATE TABLE IF NOT EXISTS rag_spaces (
                id uuid PRIMARY KEY, fingerprint text UNIQUE NOT NULL, model_id text NOT NULL,
                model_revision text NOT NULL, dimensions integer NOT NULL CHECK (dimensions BETWEEN 1 AND 16000),
                metric text NOT NULL CHECK(metric IN ('cosine','l2','inner_product')),
                normalization text NOT NULL CHECK(normalization IN ('unit','none')),
                preprocessing_revision text NOT NULL, modalities jsonb NOT NULL,
                representation text NOT NULL CHECK(representation IN ('single_vector','multi_vector')),
                document_encoder text NOT NULL, query_encoder text NOT NULL,
                created_at timestamptz NOT NULL DEFAULT now())''')
            conn.execute('''CREATE TABLE IF NOT EXISTS rag_jobs (
                id uuid PRIMARY KEY, asset_id uuid NOT NULL REFERENCES assets(id), asset_version_id uuid NOT NULL,
                space_id uuid NOT NULL REFERENCES rag_spaces(id), pipeline_revision text NOT NULL,
                request_key text NOT NULL, state text NOT NULL, attempt integer NOT NULL DEFAULT 0,
                lease_epoch integer NOT NULL DEFAULT 0, lease_until timestamptz,
                next_run_at timestamptz NOT NULL DEFAULT now(), error_code text,
                created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
                UNIQUE(asset_id, space_id, request_key))''')
            conn.execute('ALTER TABLE rag_jobs ADD COLUMN IF NOT EXISTS progress integer NOT NULL DEFAULT 0 CHECK(progress BETWEEN 0 AND 100)')
            conn.execute('''CREATE TABLE IF NOT EXISTS rag_result_sets (
                id uuid PRIMARY KEY, job_id uuid NOT NULL REFERENCES rag_jobs(id),
                asset_id uuid NOT NULL REFERENCES assets(id), asset_version_id uuid NOT NULL,
                space_id uuid NOT NULL REFERENCES rag_spaces(id), lease_epoch integer NOT NULL,
                state text NOT NULL, manifest jsonb, created_at timestamptz NOT NULL DEFAULT now(),
                activated_at timestamptz, UNIQUE(job_id, lease_epoch))''')
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS rag_active_unique ON rag_result_sets(asset_version_id, space_id) WHERE state = 'active'")
            conn.execute('''CREATE TABLE IF NOT EXISTS rag_segments (
                id uuid PRIMARY KEY, result_set_id uuid NOT NULL REFERENCES rag_result_sets(id),
                external_id text NOT NULL, kind text NOT NULL, text_content text,
                locator jsonb NOT NULL, UNIQUE(result_set_id, external_id))''')
            conn.execute('''CREATE TABLE IF NOT EXISTS rag_embeddings (
                result_set_id uuid NOT NULL REFERENCES rag_result_sets(id), segment_id uuid NOT NULL REFERENCES rag_segments(id),
                space_id uuid NOT NULL REFERENCES rag_spaces(id), ordinal integer NOT NULL, embedding vector NOT NULL,
                PRIMARY KEY(result_set_id,segment_id,ordinal))''')
            conn.execute('''CREATE OR REPLACE FUNCTION rag_check_embedding() RETURNS trigger LANGUAGE plpgsql AS $$
                DECLARE dims integer; expected_space uuid; expected_result uuid;
                BEGIN
                    SELECT dimensions INTO dims FROM rag_spaces WHERE id = NEW.space_id;
                    SELECT r.space_id, s.result_set_id INTO expected_space, expected_result FROM rag_segments s
                        JOIN rag_result_sets r ON r.id = s.result_set_id WHERE s.id = NEW.segment_id;
                    IF dims IS NULL OR vector_dims(NEW.embedding) <> dims OR expected_space IS DISTINCT FROM NEW.space_id
                       OR expected_result IS DISTINCT FROM NEW.result_set_id THEN
                        RAISE EXCEPTION 'embedding space, dimension or result mismatch';
                    END IF;
                    RETURN NEW;
                END $$''')
            conn.execute('DROP TRIGGER IF EXISTS rag_embedding_check ON rag_embeddings')
            conn.execute('CREATE TRIGGER rag_embedding_check BEFORE INSERT OR UPDATE ON rag_embeddings FOR EACH ROW EXECUTE FUNCTION rag_check_embedding()')
            conn.execute('''CREATE TABLE IF NOT EXISTS rag_batches (
                result_set_id uuid NOT NULL REFERENCES rag_result_sets(id), batch_id text NOT NULL,
                payload_hash text NOT NULL, segment_count integer NOT NULL, vector_count integer NOT NULL,
                PRIMARY KEY(result_set_id,batch_id))''')
            conn.execute('''CREATE TABLE IF NOT EXISTS rag_artifacts (
                id uuid PRIMARY KEY, result_set_id uuid NOT NULL REFERENCES rag_result_sets(id), lease_epoch integer NOT NULL,
                kind text NOT NULL, filename text NOT NULL, mime_type text NOT NULL,
                size_bytes bigint NOT NULL CHECK(size_bytes > 0), sha256 text NOT NULL,
                extractor_revision text NOT NULL, object_key text NOT NULL, state text NOT NULL,
                created_at timestamptz NOT NULL DEFAULT now())''')
            conn.execute('''CREATE TABLE IF NOT EXISTS rag_artifact_parts (
                artifact_id uuid NOT NULL REFERENCES rag_artifacts(id), part_no integer NOT NULL,
                size_bytes integer NOT NULL, sha256 text NOT NULL, PRIMARY KEY(artifact_id,part_no))''')
            conn.execute('''CREATE TABLE IF NOT EXISTS image_metadata (
                asset_version_id uuid PRIMARY KEY REFERENCES asset_versions(id),
                captured_at_local timestamp without time zone, utc_offset_minutes integer,
                latitude double precision, longitude double precision,
                updated_at timestamptz NOT NULL DEFAULT now())''')
            conn.execute('''CREATE TABLE IF NOT EXISTS rag_collection_search_policies (
                space_id uuid NOT NULL REFERENCES rag_spaces(id),
                collection_id uuid NOT NULL REFERENCES collections(id),
                min_similarity double precision NOT NULL CHECK(min_similarity BETWEEN -1 AND 1),
                updated_at timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY(space_id, collection_id))''')

    def register_space(self, model_id, model_revision, dimensions, metric, normalization,
                       preprocessing_revision, modalities, representation='single_vector',
                       document_encoder='', query_encoder=''):
        if any(not isinstance(x, str) or not x.strip() or len(x) > 256 for x in
               (model_id, model_revision, preprocessing_revision, document_encoder, query_encoder)):
            raise ValueError('모델과 인코더 식별자가 필요합니다.')
        if isinstance(dimensions, bool) or not isinstance(dimensions, int) or not 1 <= dimensions <= 16000:
            raise ValueError('지원하지 않는 차원입니다.')
        if metric not in ('cosine','l2','inner_product') or normalization not in ('unit','none') or representation not in ('single_vector','multi_vector'):
            raise ValueError('공간 설정이 올바르지 않습니다.')
        if not isinstance(modalities, list) or not modalities or any(x not in ('text','image','video','audio','document') for x in modalities):
            raise ValueError('지원 모달리티를 확인하세요.')
        modalities = sorted(set(modalities))
        identity = [model_id,model_revision,dimensions,metric,normalization,preprocessing_revision,modalities,representation,document_encoder,query_encoder]
        fingerprint = hashlib.sha256(_json(identity).encode()).hexdigest()
        with self.repository.connect() as conn:
            row = conn.execute('''INSERT INTO rag_spaces (id,fingerprint,model_id,model_revision,dimensions,metric,normalization,
                preprocessing_revision,modalities,representation,document_encoder,query_encoder)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (fingerprint)
                DO UPDATE SET fingerprint=excluded.fingerprint RETURNING *''',
                (uuid.uuid4(),fingerprint,model_id,model_revision,dimensions,metric,normalization,preprocessing_revision,
                 Jsonb(modalities),representation,document_encoder,query_encoder)).fetchone()
        return _row(row)

    def list_spaces(self):
        with self.repository.connect() as conn:
            return [_row(row) for row in conn.execute('SELECT * FROM rag_spaces ORDER BY created_at').fetchall()]

    def extend_gemini_modalities(self, space_id):
        """Add supported media kinds without changing this model's vector space."""
        space_id = _uuid(space_id)
        with self.repository.connect() as conn:
            space = conn.execute('SELECT * FROM rag_spaces WHERE id=%s FOR UPDATE', (space_id,)).fetchone()
            if not space or space['model_id'] != 'google/gemini-embedding-2' or space['representation'] != 'single_vector':
                raise ValueError('확장 가능한 Gemini 임베딩 공간이 아닙니다.')
            modalities = sorted(set(space['modalities']) | {'image', 'text', 'video', 'audio', 'document'})
            identity = [space['model_id'], space['model_revision'], space['dimensions'], space['metric'],
                        space['normalization'], space['preprocessing_revision'], modalities, space['representation'],
                        space['document_encoder'], space['query_encoder']]
            fingerprint = hashlib.sha256(_json(identity).encode()).hexdigest()
            conflict = conn.execute('SELECT id FROM rag_spaces WHERE fingerprint=%s AND id<>%s',
                                    (fingerprint, space_id)).fetchone()
            if conflict:
                raise Conflict('동일한 확장 공간이 이미 존재합니다.')
            row = conn.execute('UPDATE rag_spaces SET modalities=%s,fingerprint=%s WHERE id=%s RETURNING *',
                               (Jsonb(modalities), fingerprint, space_id)).fetchone()
            return _row(row)

    def set_collection_search_policy(self, space_id, collection_id, min_similarity):
        space_id, collection_id = _uuid(space_id), _uuid(collection_id)
        if (isinstance(min_similarity, bool) or not isinstance(min_similarity, (int, float))
                or not math.isfinite(min_similarity) or not -1 <= min_similarity <= 1):
            raise ValueError('최소 코사인 유사도는 -1~1 사이의 유한한 숫자여야 합니다.')
        with self.repository.connect() as conn:
            space = conn.execute('SELECT metric FROM rag_spaces WHERE id=%s', (space_id,)).fetchone()
            collection = conn.execute('SELECT id FROM collections WHERE id=%s', (collection_id,)).fetchone()
            if not space or not collection:
                raise ValueError('임베딩 공간 또는 컬렉션을 찾을 수 없습니다.')
            if space['metric'] != 'cosine':
                raise ValueError('최소 코사인 유사도 정책에는 코사인 공간이 필요합니다.')
            return _row(conn.execute('''INSERT INTO rag_collection_search_policies
                (space_id,collection_id,min_similarity) VALUES (%s,%s,%s)
                ON CONFLICT (space_id,collection_id) DO UPDATE SET
                  min_similarity=excluded.min_similarity,updated_at=now() RETURNING *''',
                (space_id, collection_id, float(min_similarity))).fetchone())

    def enqueue(self, asset_id, space_id, pipeline_revision='v1', request_key=None):
        asset_id, space_id = _uuid(asset_id), _uuid(space_id)
        if not isinstance(pipeline_revision, str) or not pipeline_revision or len(pipeline_revision) > 256:
            raise ValueError('파이프라인 버전이 필요합니다.')
        if request_key is None:
            request_key = str(uuid.uuid4())
        if not isinstance(request_key, str) or not request_key or len(request_key) > 256:
            raise ValueError('요청 키가 올바르지 않습니다.')
        with self.repository.connect() as conn:
            asset = conn.execute('SELECT id,version_id,kind FROM assets WHERE id=%s AND deleted_at IS NULL FOR UPDATE', (asset_id,)).fetchone()
            space = conn.execute('SELECT * FROM rag_spaces WHERE id=%s', (space_id,)).fetchone()
            if not asset or not space:
                raise ValueError('자료 또는 임베딩 공간을 찾을 수 없습니다.')
            if asset['kind'] not in space['modalities']:
                raise ValueError('공간이 지원하지 않는 자료 형식입니다.')
            previous = conn.execute('SELECT * FROM rag_jobs WHERE asset_id=%s AND space_id=%s AND request_key=%s', (asset_id,space_id,request_key)).fetchone()
            if previous:
                if previous['asset_version_id'] != asset['version_id'] or previous['pipeline_revision'] != pipeline_revision:
                    raise Conflict('요청 키가 다른 입력에 사용되었습니다.')
                return _row(previous)
            conn.execute("UPDATE rag_jobs SET state='superseded', updated_at=now() WHERE asset_id=%s AND space_id=%s AND state IN ('queued','running')", (asset_id,space_id))
            row = conn.execute('''INSERT INTO rag_jobs(id,asset_id,asset_version_id,space_id,pipeline_revision,request_key,state)
                VALUES (%s,%s,%s,%s,%s,%s,'queued') RETURNING *''',
                (uuid.uuid4(),asset_id,asset['version_id'],space_id,pipeline_revision,request_key)).fetchone()
            return _row(row)

    def status(self, job_id):
        with self.repository.connect() as conn:
            return _row(conn.execute('SELECT * FROM rag_jobs WHERE id=%s', (_uuid(job_id),)).fetchone())

    def status_for_asset(self, asset_id):
        with self.repository.connect() as conn:
            rows = conn.execute('''SELECT j.* FROM rag_jobs j JOIN assets a ON a.id=j.asset_id
                WHERE j.asset_id=%s AND a.deleted_at IS NULL ORDER BY j.created_at DESC LIMIT 100''', (_uuid(asset_id),)).fetchall()
            return [_row(row) for row in rows]

    def retry(self, job_id):
        job_id = _uuid(job_id)
        with self.repository.connect() as conn:
            reference = conn.execute('SELECT asset_id FROM rag_jobs WHERE id=%s',(job_id,)).fetchone()
            if not reference:
                raise ValueError('작업을 찾을 수 없습니다.')
            asset = conn.execute('SELECT version_id,deleted_at FROM assets WHERE id=%s FOR UPDATE',(reference['asset_id'],)).fetchone()
            job = conn.execute('SELECT * FROM rag_jobs WHERE id=%s FOR UPDATE',(job_id,)).fetchone()
            if not job or not asset or asset['deleted_at'] is not None or asset['version_id'] != job['asset_version_id']:
                raise Conflict('원본이 삭제되었거나 변경되었습니다.')
            if job['state'] != 'failed':
                raise Conflict('실패한 작업만 재시도할 수 있습니다.')
            newer = conn.execute("SELECT id FROM rag_jobs WHERE asset_id=%s AND space_id=%s AND state IN ('queued','running')", (job['asset_id'],job['space_id'])).fetchone()
            if newer:
                raise Conflict('이미 실행 중인 다른 작업이 있습니다.')
            result = conn.execute("""UPDATE rag_jobs SET state='queued',attempt=0,progress=0,error_code=NULL,
                lease_until=NULL,next_run_at=now(),updated_at=now() WHERE id=%s RETURNING *""",(job_id,)).fetchone()
            return _row(result)

    def claim(self, pipeline_revisions, space_ids):
        if not isinstance(pipeline_revisions, list) or not pipeline_revisions or any(not isinstance(x,str) or not x for x in pipeline_revisions):
            raise ValueError('지원 파이프라인 목록이 필요합니다.')
        if not isinstance(space_ids, list) or not space_ids:
            raise ValueError('지원 공간 목록이 필요합니다.')
        space_ids = [_uuid(x) for x in space_ids]
        with self.repository.connect() as conn:
            job = conn.execute('''SELECT j.* FROM rag_jobs j JOIN assets a ON a.id=j.asset_id
                WHERE j.pipeline_revision=ANY(%s) AND j.space_id=ANY(%s::uuid[])
                AND a.deleted_at IS NULL AND a.version_id=j.asset_version_id
                AND ((j.state='queued' AND j.next_run_at<=now()) OR (j.state='running' AND j.lease_until<=now()))
                ORDER BY j.created_at FOR UPDATE OF j SKIP LOCKED LIMIT 1''', (pipeline_revisions,space_ids)).fetchone()
            if not job:
                return None
            if job['attempt'] >= 4:
                conn.execute("UPDATE rag_jobs SET state='failed',error_code='lease_retries_exhausted',updated_at=now() WHERE id=%s", (job['id'],))
                return None
            row = conn.execute("""UPDATE rag_jobs SET state='running',attempt=attempt+1,lease_epoch=lease_epoch+1,
                lease_until=now()+interval '5 minutes',updated_at=now() WHERE id=%s RETURNING *""", (job['id'],)).fetchone()
            return _row(row)

    def _lease(self, conn, job_id, epoch):
        if isinstance(epoch,bool) or not isinstance(epoch,int) or epoch < 1:
            raise ValueError('작업 임대 번호가 올바르지 않습니다.')
        job_id = _uuid(job_id)
        reference = conn.execute('SELECT asset_id FROM rag_jobs WHERE id=%s', (job_id,)).fetchone()
        if not reference:
            raise ValueError('작업을 찾을 수 없습니다.')
        asset = conn.execute('SELECT deleted_at,version_id FROM assets WHERE id=%s FOR UPDATE', (reference['asset_id'],)).fetchone()
        job = conn.execute('SELECT *, (lease_until > clock_timestamp()) AS lease_valid FROM rag_jobs WHERE id=%s FOR UPDATE', (job_id,)).fetchone()
        if not job:
            raise ValueError('작업을 찾을 수 없습니다.')
        if not asset or job['state'] != 'running' or job['lease_epoch'] != epoch or not job['lease_valid'] or asset['deleted_at'] is not None or asset['version_id'] != job['asset_version_id']:
            raise Conflict('작업 임대가 만료되었거나 원본이 변경되었습니다.')
        return job

    def heartbeat(self, job_id, epoch, progress=None):
        if progress is not None and (isinstance(progress,bool) or not isinstance(progress,int) or not 0 <= progress <= 100):
            raise ValueError('진행률은 0~100 정수여야 합니다.')
        with self.repository.connect() as conn:
            job = self._lease(conn,job_id,epoch)
            return _row(conn.execute("UPDATE rag_jobs SET lease_until=now()+interval '5 minutes',progress=COALESCE(%s,progress),updated_at=now() WHERE id=%s RETURNING *", (progress,job['id'])).fetchone())

    def source(self, job_id, epoch):
        with self.repository.connect() as conn:
            job = self._lease(conn,job_id,epoch)
            return _row(conn.execute('SELECT id,version_id,kind,title,filename,mime_type,text_body,object_key FROM assets WHERE id=%s', (job['asset_id'],)).fetchone())

    def record_image_metadata(self, job_id, epoch, captured_at_local, utc_offset_minutes, latitude, longitude):
        if captured_at_local is not None:
            if not isinstance(captured_at_local, str):
                raise ValueError('촬영 시각이 올바르지 않습니다.')
            captured_at_local = datetime.fromisoformat(captured_at_local)
            if captured_at_local.tzinfo is not None:
                raise ValueError('촬영 시각에는 시간대를 포함하지 마세요.')
        if utc_offset_minutes is not None and (captured_at_local is None or isinstance(utc_offset_minutes, bool)
                or not isinstance(utc_offset_minutes, int) or not -840 <= utc_offset_minutes <= 840):
            raise ValueError('촬영 시간대가 올바르지 않습니다.')
        if (latitude is None) != (longitude is None):
            raise ValueError('위도와 경도를 함께 입력하세요.')
        for value, bound in ((latitude, 90), (longitude, 180)):
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                      or not math.isfinite(value) or abs(value) > bound):
                raise ValueError('촬영 좌표가 올바르지 않습니다.')
        with self.repository.connect() as conn:
            job = self._lease(conn, job_id, epoch)
            asset = conn.execute('SELECT kind FROM assets WHERE id=%s', (job['asset_id'],)).fetchone()
            if asset['kind'] != 'image':
                raise ValueError('사진에만 촬영 정보를 저장할 수 있습니다.')
            return _row(conn.execute('''INSERT INTO image_metadata(asset_version_id,captured_at_local,utc_offset_minutes,latitude,longitude)
                VALUES (%s,%s,%s,%s,%s) ON CONFLICT (asset_version_id) DO UPDATE SET
                captured_at_local=excluded.captured_at_local, utc_offset_minutes=excluded.utc_offset_minutes,
                latitude=excluded.latitude, longitude=excluded.longitude, updated_at=now() RETURNING *''',
                (job['asset_version_id'],captured_at_local,utc_offset_minutes,latitude,longitude)).fetchone())

    def image_metadata(self, asset_id, version_id):
        with self.repository.connect() as conn:
            return _row(conn.execute('''SELECT m.* FROM image_metadata m JOIN asset_versions v ON v.id=m.asset_version_id
                JOIN assets a ON a.id=v.asset_id WHERE a.id=%s AND v.id=%s AND a.deleted_at IS NULL''',
                (_uuid(asset_id), _uuid(version_id))).fetchone())

    def create_result(self, job_id, epoch):
        with self.repository.connect() as conn:
            job = self._lease(conn,job_id,epoch)
            result = conn.execute('''INSERT INTO rag_result_sets(id,job_id,asset_id,asset_version_id,space_id,lease_epoch,state)
                VALUES (%s,%s,%s,%s,%s,%s,'staging') ON CONFLICT(job_id,lease_epoch)
                DO UPDATE SET job_id=excluded.job_id RETURNING *''',
                (uuid.uuid4(),job['id'],job['asset_id'],job['asset_version_id'],job['space_id'],epoch)).fetchone()
            return _row(result)

    def _result(self, conn, result_id, epoch):
        result = conn.execute('SELECT * FROM rag_result_sets WHERE id=%s FOR UPDATE', (_uuid(result_id),)).fetchone()
        if not result:
            raise ValueError('결과 묶음을 찾을 수 없습니다.')
        self._lease(conn,result['job_id'],epoch)
        if result['lease_epoch'] != epoch or result['state'] != 'staging':
            raise Conflict('결과 묶음이 현재 임대에 속하지 않습니다.')
        return result

    def submit_batch(self, result_id, batch_id, epoch, source_version_id, space_id, segments):
        if not isinstance(batch_id,str) or not batch_id or len(batch_id) > 128:
            raise ValueError('배치 ID가 올바르지 않습니다.')
        if not isinstance(segments,list) or not 1 <= len(segments) <= 256:
            raise ValueError('구간 개수가 올바르지 않습니다.')
        body = {'lease_epoch':epoch,'source_version_id':_uuid(source_version_id),'space_id':_uuid(space_id),'segments':segments}
        try:
            encoded = _json(body).encode()
        except (ValueError,TypeError) as exc:
            raise ValueError('배치 데이터가 올바르지 않습니다.') from exc
        if len(encoded) > 8 * 1024 * 1024:
            raise ValueError('배치 크기가 너무 큽니다.')
        digest = hashlib.sha256(encoded).hexdigest()
        with self.repository.connect() as conn:
            result = self._result(conn,result_id,epoch)
            if str(result['asset_version_id']) != body['source_version_id'] or str(result['space_id']) != body['space_id']:
                raise Conflict('원본 버전 또는 임베딩 공간이 다릅니다.')
            old = conn.execute('SELECT * FROM rag_batches WHERE result_set_id=%s AND batch_id=%s', (result['id'],batch_id)).fetchone()
            if old:
                if old['payload_hash'] != digest:
                    raise Conflict('배치 ID가 다른 내용에 사용되었습니다.')
                return _row(old)
            space = conn.execute('SELECT * FROM rag_spaces WHERE id=%s', (result['space_id'],)).fetchone()
            total = 0
            for entry in segments:
                if not isinstance(entry,dict) or not isinstance(entry.get('external_id'),str) or not 1 <= len(entry['external_id']) <= 256:
                    raise ValueError('구간 ID를 확인하세요.')
                if entry.get('kind') not in ('text','image','video','audio','document') or not isinstance(entry.get('locator'),dict):
                    raise ValueError('구간 종류 또는 위치가 올바르지 않습니다.')
                loc = entry['locator']
                if 'start_ms' in loc or 'end_ms' in loc:
                    if any(isinstance(loc.get(k),bool) or not isinstance(loc.get(k),int) for k in ('start_ms','end_ms')) or loc['start_ms'] < 0 or loc['end_ms'] <= loc['start_ms']:
                        raise ValueError('시간 구간이 올바르지 않습니다.')
                if 'page' in loc and (isinstance(loc['page'],bool) or not isinstance(loc['page'],int) or loc['page'] < 1):
                    raise ValueError('페이지가 올바르지 않습니다.')
                if 'char_start' in loc or 'char_end' in loc:
                    if any(isinstance(loc.get(k),bool) or not isinstance(loc.get(k),int) for k in ('char_start','char_end')) or loc['char_start'] < 0 or loc['char_end'] <= loc['char_start']:
                        raise ValueError('텍스트 구간이 올바르지 않습니다.')
                if 'bbox' in loc:
                    box = loc['bbox']
                    if not isinstance(box,list) or len(box)!=4 or any(isinstance(x,bool) or not isinstance(x,(int,float)) or not math.isfinite(x) or x<0 or x>1 for x in box) or box[0]>=box[2] or box[1]>=box[3]:
                        raise ValueError('이미지 영역이 올바르지 않습니다.')
                text = entry.get('text')
                if text is not None and (not isinstance(text,str) or len(text.encode()) > 256*1024):
                    raise ValueError('구간 텍스트가 올바르지 않습니다.')
                vectors = validate_vectors(entry.get('vectors'),space)
                total += len(vectors)
                if total > 256:
                    raise ValueError('배치의 벡터는 256개 이하여야 합니다.')
                sid = uuid.uuid4()
                conn.execute('INSERT INTO rag_segments(id,result_set_id,external_id,kind,text_content,locator) VALUES(%s,%s,%s,%s,%s,%s)',
                             (sid,result['id'],entry['external_id'],entry['kind'],text,Jsonb(loc)))
                for ordinal, values in enumerate(vectors):
                    conn.execute('INSERT INTO rag_embeddings(result_set_id,segment_id,space_id,ordinal,embedding) VALUES (%s,%s,%s,%s,%s::vector)',
                                 (result['id'],sid,result['space_id'],ordinal,_vector(values)))
            row = conn.execute('INSERT INTO rag_batches(result_set_id,batch_id,payload_hash,segment_count,vector_count) VALUES(%s,%s,%s,%s,%s) RETURNING *',
                               (result['id'],batch_id,digest,len(segments),total)).fetchone()
            return _row(row)

    def complete(self, result_id, epoch, manifest):
        if not isinstance(manifest,dict) or not isinstance(manifest.get('batches'),list):
            raise ValueError('결과 manifest가 올바르지 않습니다.')
        batches = manifest['batches']
        if not batches or any(not isinstance(x,str) or not x for x in batches) or len(set(batches)) != len(batches):
            raise ValueError('배치 목록이 올바르지 않습니다.')
        if any(isinstance(manifest.get(k),bool) or not isinstance(manifest.get(k),int) or manifest[k]<0 for k in ('segment_count','vector_count')):
            raise ValueError('결과 수가 올바르지 않습니다.')
        artifact_ids = manifest.get('artifact_ids', [])
        if not isinstance(artifact_ids,list) or len(set(map(str,artifact_ids)))!=len(artifact_ids):
            raise ValueError('파생물 목록이 올바르지 않습니다.')
        artifact_ids = sorted(_uuid(item) for item in artifact_ids)
        artifact_count = manifest.get('artifact_count',len(artifact_ids))
        if isinstance(artifact_count,bool) or not isinstance(artifact_count,int) or artifact_count != len(artifact_ids):
            raise ValueError('파생물 개수가 올바르지 않습니다.')
        manifest = {'batches':sorted(batches),'segment_count':manifest['segment_count'],'vector_count':manifest['vector_count'],
                    'artifact_ids':artifact_ids,'artifact_count':artifact_count}
        with self.repository.connect() as conn:
            prior = conn.execute('SELECT * FROM rag_result_sets WHERE id=%s', (_uuid(result_id),)).fetchone()
            if prior and prior['state'] in ('active','superseded') and prior['lease_epoch']==epoch and prior['manifest']==manifest:
                asset = conn.execute('SELECT version_id,deleted_at FROM assets WHERE id=%s FOR UPDATE', (prior['asset_id'],)).fetchone()
                if not asset or asset['deleted_at'] is not None or asset['version_id'] != prior['asset_version_id']:
                    raise Conflict('원본이 삭제되었거나 변경되었습니다.')
                return _row(prior)
            result = self._result(conn,result_id,epoch)
            rows = conn.execute('SELECT batch_id,segment_count,vector_count FROM rag_batches WHERE result_set_id=%s', (result['id'],)).fetchall()
            if sorted(r['batch_id'] for r in rows)!=manifest['batches'] or sum(r['segment_count'] for r in rows)!=manifest['segment_count'] or sum(r['vector_count'] for r in rows)!=manifest['vector_count']:
                raise Conflict('결과 배치 또는 개수가 일치하지 않습니다.')
            artifacts = conn.execute('SELECT id,state FROM rag_artifacts WHERE result_set_id=%s', (result['id'],)).fetchall()
            if sorted(str(a['id']) for a in artifacts) != artifact_ids or any(a['state'] != 'ready' for a in artifacts):
                raise Conflict('파생물 목록이 일치하지 않거나 업로드가 완료되지 않았습니다.')
            for segment in conn.execute('SELECT locator FROM rag_segments WHERE result_set_id=%s', (result['id'],)).fetchall():
                linked_id = segment['locator'].get('artifact_id')
                if linked_id is not None and _uuid(linked_id) not in artifact_ids:
                    raise Conflict('구간이 현재 결과에 없는 파생물을 참조합니다.')
            conn.execute("UPDATE rag_result_sets SET state='superseded' WHERE asset_version_id=%s AND space_id=%s AND state='active'", (result['asset_version_id'],result['space_id']))
            result = conn.execute("UPDATE rag_result_sets SET state='active',manifest=%s,activated_at=now() WHERE id=%s RETURNING *", (Jsonb(manifest),result['id'])).fetchone()
            conn.execute("UPDATE rag_jobs SET state='succeeded',progress=100,lease_until=NULL,updated_at=now() WHERE id=%s", (result['job_id'],))
            return _row(result)

    def fail(self, job_id, epoch, error_code, retryable=False):
        if not isinstance(error_code,str) or not error_code or len(error_code)>100:
            raise ValueError('오류 코드가 필요합니다.')
        with self.repository.connect() as conn:
            job = self._lease(conn,job_id,epoch)
            retry = bool(retryable) and job['attempt'] < 4
            delay = (30,120,600)[min(job['attempt']-1,2)]
            row = conn.execute("""UPDATE rag_jobs SET state=%s,error_code=%s,lease_until=NULL,
                next_run_at=now()+(%s * interval '1 second'),updated_at=now() WHERE id=%s RETURNING *""",
                ('queued' if retry else 'failed',error_code,delay if retry else 0,job['id'])).fetchone()
            return _row(row)

    def begin_artifact(self, result_id, epoch, kind, filename, mime_type, size_bytes, sha256, extractor_revision, artifact_id=None):
        if not isinstance(kind,str) or not 1 <= len(kind) <= 100 or not isinstance(filename,str) or not 1 <= len(filename) <= 255 or '/' in filename or '\\' in filename:
            raise ValueError('파생물 종류 또는 파일 이름이 올바르지 않습니다.')
        if not isinstance(mime_type,str) or not 1 <= len(mime_type) <= 150 or not isinstance(extractor_revision,str) or not 1 <= len(extractor_revision) <= 256:
            raise ValueError('파생물 형식 또는 추출기 버전이 올바르지 않습니다.')
        if isinstance(size_bytes,bool) or not isinstance(size_bytes,int) or not 1 <= size_bytes <= 10*1024**3:
            raise ValueError('파생물 크기가 올바르지 않습니다.')
        if not isinstance(sha256,str) or len(sha256)!=64 or any(c not in '0123456789abcdef' for c in sha256):
            raise ValueError('파생물 SHA-256이 올바르지 않습니다.')
        artifact_id = _uuid(artifact_id or uuid.uuid4())
        with self.repository.connect() as conn:
            result = self._result(conn,result_id,epoch)
            existing = conn.execute('SELECT * FROM rag_artifacts WHERE id=%s FOR UPDATE', (artifact_id,)).fetchone()
            if existing:
                proposed = (str(result['id']),epoch,kind,filename,mime_type,size_bytes,sha256,extractor_revision)
                actual = (str(existing['result_set_id']),existing['lease_epoch'],existing['kind'],existing['filename'],existing['mime_type'],existing['size_bytes'],existing['sha256'],existing['extractor_revision'])
                if proposed != actual:
                    raise Conflict('파생물 ID가 다른 내용에 사용되었습니다.')
                return _row(existing)
            key = f"derived/{result['asset_version_id']}/{result['id']}/{artifact_id}"
            row = conn.execute('''INSERT INTO rag_artifacts(id,result_set_id,lease_epoch,kind,filename,mime_type,size_bytes,sha256,extractor_revision,object_key,state)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'uploading') RETURNING *''',
                (artifact_id,result['id'],epoch,kind,filename,mime_type,size_bytes,sha256,extractor_revision,key)).fetchone()
            return _row(row)

    def _artifact(self,conn,artifact_id,epoch):
        artifact = conn.execute('SELECT * FROM rag_artifacts WHERE id=%s FOR UPDATE', (_uuid(artifact_id),)).fetchone()
        if not artifact:
            raise ValueError('파생물을 찾을 수 없습니다.')
        self._result(conn,artifact['result_set_id'],epoch)
        if artifact['lease_epoch'] != epoch:
            raise Conflict('파생물이 현재 임대에 속하지 않습니다.')
        return artifact

    def put_artifact_part(self, artifact_id, epoch, part_no, content, sha256):
        if isinstance(part_no,bool) or not isinstance(part_no,int) or part_no < 0 or not isinstance(content,bytes):
            raise ValueError('파생물 조각이 올바르지 않습니다.')
        if not isinstance(sha256,str) or not hmac.compare_digest(hashlib.sha256(content).hexdigest(),sha256):
            raise ValueError('파생물 조각 해시가 일치하지 않습니다.')
        chunk = 8*1024*1024
        with self.repository.connect() as conn:
            artifact = self._artifact(conn,artifact_id,epoch)
            if artifact['state'] != 'uploading':
                raise Conflict('업로드 중인 파생물이 아닙니다.')
            parts = conn.execute('SELECT * FROM rag_artifact_parts WHERE artifact_id=%s ORDER BY part_no', (artifact['id'],)).fetchall()
            if part_no < len(parts):
                if parts[part_no]['sha256'] != sha256 or parts[part_no]['size_bytes'] != len(content):
                    raise Conflict('같은 조각 번호의 내용이 다릅니다.')
                return {'part_no':part_no,'sha256':sha256}
            expected = min(chunk,artifact['size_bytes']-part_no*chunk)
            if part_no != len(parts) or expected <= 0 or len(content) != expected:
                raise Conflict('파생물 조각 순서 또는 크기가 다릅니다.')
            directory = self.repository.media_root / 'staging' / 'rag' / str(artifact['id'])
            directory.mkdir(parents=True,exist_ok=True)
            fd, name = tempfile.mkstemp(prefix='.part-',dir=directory)
            try:
                with os.fdopen(fd,'wb') as out:
                    out.write(content)
                    out.flush()
                    os.fsync(out.fileno())
                os.replace(name,directory / str(part_no))
            finally:
                if os.path.exists(name):
                    os.unlink(name)
            conn.execute('INSERT INTO rag_artifact_parts(artifact_id,part_no,size_bytes,sha256) VALUES(%s,%s,%s,%s)',
                         (artifact['id'],part_no,len(content),sha256))
            return {'part_no':part_no,'sha256':sha256}

    def complete_artifact(self, artifact_id, epoch):
        chunk = 8*1024*1024
        with self.repository.connect() as conn:
            artifact = self._artifact(conn,artifact_id,epoch)
            if artifact['state'] == 'ready':
                return _row(artifact)
            parts = conn.execute('SELECT * FROM rag_artifact_parts WHERE artifact_id=%s ORDER BY part_no', (artifact['id'],)).fetchall()
            count = (artifact['size_bytes']+chunk-1)//chunk
            if len(parts)!=count or [x['part_no'] for x in parts]!=list(range(count)):
                raise Conflict('파생물 조각이 완성되지 않았습니다.')
            directory = self.repository.media_root / 'staging' / 'rag' / str(artifact['id'])
            destination = self.repository.media_root / artifact['object_key']
            destination.parent.mkdir(parents=True,exist_ok=True)
            fd, temp = tempfile.mkstemp(prefix='.artifact-',dir=destination.parent)
            total, digest = 0, hashlib.sha256()
            try:
                with os.fdopen(fd,'wb') as out:
                    for part in parts:
                        pdigest = hashlib.sha256()
                        with (directory / str(part['part_no'])).open('rb') as source:
                            while content := source.read(1024*1024):
                                out.write(content)
                                total += len(content)
                                digest.update(content)
                                pdigest.update(content)
                        if pdigest.hexdigest()!=part['sha256']:
                            raise ValueError('파생물 조각 해시가 일치하지 않습니다.')
                    out.flush()
                    os.fsync(out.fileno())
                if total!=artifact['size_bytes'] or digest.hexdigest()!=artifact['sha256']:
                    raise ValueError('파생물 파일 크기 또는 해시가 일치하지 않습니다.')
                # Recheck the lease after file I/O before exposing the artifact.
                self._lease(conn, conn.execute('SELECT job_id FROM rag_result_sets WHERE id=%s',(artifact['result_set_id'],)).fetchone()['job_id'], epoch)
                os.replace(temp,destination)
                row = conn.execute("UPDATE rag_artifacts SET state='ready' WHERE id=%s RETURNING *", (artifact['id'],)).fetchone()
                saved = _row(row)
            finally:
                if os.path.exists(temp):
                    os.unlink(temp)
        shutil.rmtree(directory,ignore_errors=True)
        return saved

    def list_artifacts(self, asset_id):
        with self.repository.connect() as conn:
            rows = conn.execute('''SELECT t.id,t.kind,t.filename,t.mime_type,t.size_bytes,t.sha256,t.extractor_revision,t.created_at
                FROM rag_artifacts t JOIN rag_result_sets r ON r.id=t.result_set_id JOIN assets a ON a.id=r.asset_id
                WHERE a.id=%s AND a.deleted_at IS NULL AND a.version_id=r.asset_version_id AND r.state='active' AND t.state='ready'
                ORDER BY t.created_at,t.id''', (_uuid(asset_id),)).fetchall()
            return [_row(row) for row in rows]

    def artifact_status(self, artifact_id, epoch):
        with self.repository.connect() as conn:
            artifact = self._artifact(conn,artifact_id,epoch)
            result = _row(artifact)
            result.pop('object_key',None)
            result['parts'] = [_row(part) for part in conn.execute('SELECT part_no,size_bytes,sha256 FROM rag_artifact_parts WHERE artifact_id=%s ORDER BY part_no', (artifact['id'],)).fetchall()]
            return result

    def get_artifact(self, artifact_id):
        with self.repository.connect() as conn:
            row = conn.execute('''SELECT t.*,r.asset_id,r.asset_version_id FROM rag_artifacts t
                JOIN rag_result_sets r ON r.id=t.result_set_id JOIN assets a ON a.id=r.asset_id
                WHERE t.id=%s AND a.deleted_at IS NULL AND a.version_id=r.asset_version_id AND r.state='active' AND t.state='ready' ''',
                (_uuid(artifact_id),)).fetchone()
            return _row(row)

    def browse_collection_images(self, space_id, collection_id, limit=3, require_unique_policy=False):
        """Browse indexed images; optionally require the unique automatic policy scope."""
        space_id, collection_id = _uuid(space_id), _uuid(collection_id)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError('검색 개수는 1~100이어야 합니다.')
        with self.repository.connect() as conn:
            conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
            if require_unique_policy:
                candidates = conn.execute('''WITH active_images AS (
                    SELECT DISTINCT a.id FROM rag_embeddings e
                    JOIN rag_result_sets r ON r.id=e.result_set_id JOIN assets a ON a.id=r.asset_id
                    WHERE e.space_id=%s AND r.state='active' AND a.deleted_at IS NULL
                      AND a.version_id=r.asset_version_id AND a.kind='image')
                    SELECT p.collection_id FROM rag_collection_search_policies p
                    WHERE p.space_id=%s AND EXISTS (SELECT 1 FROM active_images)
                      AND NOT EXISTS (SELECT 1 FROM active_images i WHERE NOT EXISTS (
                        SELECT 1 FROM asset_collections ac
                        WHERE ac.asset_id=i.id AND ac.collection_id=p.collection_id))
                    ORDER BY p.collection_id LIMIT 2''', (space_id,space_id)).fetchall()
                if len(candidates) != 1 or str(candidates[0]['collection_id']) != collection_id:
                    return None
            rows = conn.execute('''SELECT a.id AS asset_id,a.version_id,a.title,a.kind,
                    m.captured_at_local,m.utc_offset_minutes,m.latitude,m.longitude
                FROM assets a LEFT JOIN image_metadata m ON m.asset_version_id=a.version_id
                WHERE a.kind='image' AND a.deleted_at IS NULL
                  AND EXISTS (SELECT 1 FROM asset_collections ac WHERE ac.asset_id=a.id AND ac.collection_id=%s)
                  AND EXISTS (SELECT 1 FROM rag_result_sets r JOIN rag_embeddings e ON e.result_set_id=r.id
                    WHERE r.asset_id=a.id AND r.asset_version_id=a.version_id AND r.state='active' AND e.space_id=%s)
                ORDER BY a.created_at DESC,a.id DESC LIMIT %s''', (collection_id,space_id,limit)).fetchall()
            return {'space_id':space_id,'collection_id':collection_id,'mode':'browse',
                    'status':'ok' if rows else 'no_match','results':[_row(row) for row in rows]}

    def search_vector(self, space_id, values, limit=20, kinds=None, collection_id=None, captured_from=None, captured_to=None,
                      auto_collection_policy=False, use_collection_policy=True):
        space_id = _uuid(space_id)
        if isinstance(limit,bool) or not isinstance(limit,int) or not 1 <= limit <= 100:
            raise ValueError('검색 개수는 1~100이어야 합니다.')
        if kinds is not None and (not isinstance(kinds,list) or any(x not in ('text','image','video','audio','document') for x in kinds)):
            raise ValueError('자료 유형이 올바르지 않습니다.')
        if collection_id is not None:
            collection_id = _uuid(collection_id)
        try:
            start_date = date.fromisoformat(captured_from) if captured_from is not None else None
            end_date = date.fromisoformat(captured_to) if captured_to is not None else None
        except (ValueError, TypeError) as exc:
            raise ValueError('촬영 날짜는 YYYY-MM-DD 형식이어야 합니다.') from exc
        if (captured_from is not None and captured_from != start_date.isoformat()) or (captured_to is not None and captured_to != end_date.isoformat()):
            raise ValueError('촬영 날짜는 YYYY-MM-DD 형식이어야 합니다.')
        if start_date and end_date and start_date > end_date:
            raise ValueError('촬영 시작 날짜는 종료 날짜보다 늦을 수 없습니다.')
        with self.repository.connect() as conn:
            if auto_collection_policy and not collection_id:
                # Keep policy selection and retrieval on one consistent snapshot.
                conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
            space = conn.execute('SELECT * FROM rag_spaces WHERE id=%s', (space_id,)).fetchone()
            if not space:
                raise ValueError('임베딩 공간을 찾을 수 없습니다.')
            if space['representation'] != 'single_vector':
                raise ValueError('다중 벡터 검색은 모델별 재정렬 계약이 필요합니다.')
            vector = validate_vectors([{'ordinal':0,'values':values}],space)[0]
            op = {'cosine':'<=>','l2':'<->','inner_product':'<#>'}[space['metric']]
            policy_collection_id = collection_id
            auto_selected = False
            if auto_collection_policy and not collection_id:
                candidates = conn.execute('''WITH active_images AS (
                    SELECT DISTINCT a.id FROM rag_embeddings e
                    JOIN rag_result_sets r ON r.id=e.result_set_id
                    JOIN assets a ON a.id=r.asset_id
                    WHERE e.space_id=%s AND r.state='active' AND a.deleted_at IS NULL
                      AND a.version_id=r.asset_version_id AND a.kind='image')
                    SELECT p.collection_id FROM rag_collection_search_policies p
                    WHERE p.space_id=%s AND EXISTS (SELECT 1 FROM active_images)
                      AND NOT EXISTS (SELECT 1 FROM active_images i WHERE NOT EXISTS (
                        SELECT 1 FROM asset_collections ac
                        WHERE ac.asset_id=i.id AND ac.collection_id=p.collection_id))
                    ORDER BY p.collection_id LIMIT 2''', (space_id,space_id)).fetchall()
                if len(candidates) == 1:
                    policy_collection_id = str(candidates[0]['collection_id'])
                    auto_selected = True
            policy = conn.execute('SELECT min_similarity FROM rag_collection_search_policies WHERE space_id=%s AND collection_id=%s',
                                  (space_id,policy_collection_id)).fetchone() if policy_collection_id and use_collection_policy else None
            threshold = policy['min_similarity'] if policy else None
            if threshold is not None and space['metric'] != 'cosine':
                raise ValueError('코사인 유사도 정책과 검색 공간이 일치하지 않습니다.')
            where = 'AND a.kind=ANY(%s)' if kinds else ''
            if collection_id:
                where += ' AND EXISTS (SELECT 1 FROM asset_collections ac WHERE ac.asset_id=a.id AND ac.collection_id=%s)'
            if start_date:
                where += ' AND m.captured_at_local >= %s::date'
            if end_date:
                where += " AND m.captured_at_local < %s::date + interval '1 day'"
            if threshold is not None:
                where += ' AND 1 - (e.embedding <=> q.v) >= %s'
            params = [space_id,_vector(vector)] + ([kinds] if kinds else []) + ([collection_id] if collection_id else []) + ([start_date] if start_date else []) + ([end_date] if end_date else []) + ([threshold] if threshold is not None else []) + [limit]
            # Operator is selected from a fixed allowlist, never user-provided SQL.
            rows = conn.execute(f'''SELECT * FROM (SELECT a.id AS asset_id,a.version_id,a.title,a.kind,s.id AS segment_id,
                s.text_content AS text,s.locator,r.space_id,m.captured_at_local,m.utc_offset_minutes,m.latitude,m.longitude,e.embedding {op} q.v AS distance,
                row_number() OVER (PARTITION BY CASE WHEN a.kind='image' THEN a.id ELSE s.id END
                                   ORDER BY e.embedding {op} q.v, s.id) AS result_rank
                FROM rag_embeddings e JOIN rag_segments s ON s.id=e.segment_id
                JOIN rag_result_sets r ON r.id=e.result_set_id JOIN assets a ON a.id=r.asset_id
                LEFT JOIN image_metadata m ON m.asset_version_id=a.version_id
                CROSS JOIN (SELECT %s::uuid AS space,%s::vector AS v) q
                WHERE e.space_id=q.space AND r.state='active' AND a.deleted_at IS NULL
                AND a.version_id=r.asset_version_id {where}) ranked
                WHERE result_rank=1 ORDER BY distance, segment_id LIMIT %s''', params).fetchall()
            results = [_row(row) for row in rows]
            for row in results:
                row.pop('result_rank', None)
            response = {'space_id':space_id,'metric':space['metric'],'results':results}
            if threshold is not None:
                response['min_similarity'] = threshold
                response['policy_collection_id'] = policy_collection_id
                response['policy_auto_selected'] = auto_selected
            elif auto_collection_policy:
                response['relevance_unverified'] = True
            if (start_date or end_date) and (not kinds or 'image' in kinds):
                scope = (' AND EXISTS (SELECT 1 FROM asset_collections ac'
                         ' WHERE ac.asset_id=a.id AND ac.collection_id=%s)') if collection_id else ''
                count_params = [space_id] + ([collection_id] if collection_id else [])
                if threshold is not None:
                    scope += ' AND 1 - (e.embedding <=> %s::vector) >= %s'
                    count_params.extend([_vector(vector), threshold])
                count = conn.execute(f'''SELECT COUNT(DISTINCT a.id) AS n
                    FROM rag_embeddings e JOIN rag_result_sets r ON r.id=e.result_set_id
                    JOIN assets a ON a.id=r.asset_id
                    LEFT JOIN image_metadata m ON m.asset_version_id=a.version_id
                    WHERE e.space_id=%s AND r.state='active' AND a.deleted_at IS NULL
                      AND a.version_id=r.asset_version_id AND a.kind='image'
                      AND m.captured_at_local IS NULL {scope}''', count_params).fetchone()['n']
                if count:
                    response['missing_capture_date_count'] = count
            return response


def worker_routes(service, worker_token, public_base_url):
    """Worker API: each request is authenticated with its own service token."""
    if not isinstance(worker_token, str) or len(worker_token) < 32 or not worker_token.isascii() or any(c.isspace() for c in worker_token):
        raise ValueError('작업자 토큰은 공백 없는 32자 이상 ASCII여야 합니다.')
    secret = worker_token.encode()
    base = public_base_url.rstrip('/')

    async def handler(request, action):
        credentials = request.headers.getlist('authorization')
        if len(credentials) != 1 or not credentials[0].startswith('Bearer ') or not hmac.compare_digest(credentials[0][7:].encode(), secret):
            return JSONResponse({'error':'worker authentication required'}, status_code=401, headers={'WWW-Authenticate':'Bearer'})
        try:
            if action == 'source':
                epoch = int(request.query_params.get('lease_epoch',''))
                src = await run_in_threadpool(service.source,request.path_params['job_id'],epoch)
                if src['kind'] == 'text':
                    return Response(src['text_body'],media_type='text/plain; charset=utf-8',headers={'Cache-Control':'no-store'})
                path = service.repository.asset_path(src)
                if not path.is_file():
                    return JSONResponse({'error':'source file not found'},status_code=404)
                return FileResponse(path,media_type=src['mime_type'],filename=src.get('filename') or 'source',headers={'Cache-Control':'no-store'})
            raw = bytearray()
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw) > 8*1024*1024:
                    return JSONResponse({'error':'request too large'},status_code=413)
            if action == 'artifact_part':
                epoch = int(request.headers.get('x-lease-epoch',''))
                result = await run_in_threadpool(service.put_artifact_part,request.path_params['artifact_id'],epoch,
                                                 int(request.path_params['part_no']),bytes(raw),request.headers.get('x-part-sha256',''))
                return JSONResponse(result,headers={'Cache-Control':'no-store'})
            body = json.loads(raw or b'{}')
            if not isinstance(body,dict):
                raise ValueError('요청은 JSON 객체여야 합니다.')
            job_id = request.path_params.get('job_id')
            result_id = request.path_params.get('result_id')
            epoch = body.get('lease_epoch')
            if action == 'claim':
                result = await run_in_threadpool(service.claim,body.get('pipeline_revisions'),body.get('space_ids'))
                if result is None:
                    return Response(status_code=204)
            elif action == 'heartbeat':
                result = await run_in_threadpool(service.heartbeat,job_id,epoch,body.get('progress'))
            elif action == 'source_grant':
                src = await run_in_threadpool(service.source,job_id,epoch)
                result = {k:v for k,v in src.items() if k != 'object_key'}
                if src['kind'] != 'text':
                    result['source_url'] = f'{base}/api/v1/worker/jobs/{job_id}/source?lease_epoch={epoch}'
            elif action == 'metadata':
                result = await run_in_threadpool(service.record_image_metadata,job_id,epoch,body.get('captured_at_local'),
                    body.get('utc_offset_minutes'),body.get('latitude'),body.get('longitude'))
            elif action == 'create_result':
                result = await run_in_threadpool(service.create_result,job_id,epoch)
            elif action == 'batch':
                result = await run_in_threadpool(service.submit_batch,result_id,request.path_params['batch_id'],epoch,
                                                 body.get('source_version_id'),body.get('space_id'),body.get('segments'))
            elif action == 'begin_artifact':
                result = await run_in_threadpool(service.begin_artifact,result_id,epoch,body.get('kind'),body.get('filename'),
                    body.get('mime_type'),body.get('size_bytes'),body.get('sha256'),body.get('extractor_revision'),body.get('artifact_id'))
                result.pop('object_key',None)
            elif action == 'artifact_status':
                result = await run_in_threadpool(service.artifact_status,request.path_params['artifact_id'],epoch)
            elif action == 'complete_artifact':
                result = await run_in_threadpool(service.complete_artifact,request.path_params['artifact_id'],epoch)
                result.pop('object_key',None)
            elif action == 'complete':
                result = await run_in_threadpool(service.complete,result_id,epoch,body.get('manifest'))
            elif action == 'fail':
                result = await run_in_threadpool(service.fail,job_id,epoch,body.get('error_code'),body.get('retryable',False))
            else:
                return Response(status_code=404)
            return JSONResponse(result,headers={'Cache-Control':'no-store'})
        except UniqueViolation:
            return JSONResponse({'error':'중복된 구간 또는 요청입니다.'},status_code=409)
        except Conflict as exc:
            return JSONResponse({'error':str(exc)},status_code=409)
        except (ValueError,TypeError,UnicodeDecodeError,json.JSONDecodeError) as exc:
            return JSONResponse({'error':str(exc)},status_code=422)

    def endpoint(action):
        async def wrapped(request):
            return await handler(request,action)
        return wrapped

    return [
        Route('/api/v1/worker/jobs/claim',endpoint('claim'),methods=['POST']),
        Route('/api/v1/worker/jobs/{job_id}/heartbeat',endpoint('heartbeat'),methods=['POST']),
        Route('/api/v1/worker/jobs/{job_id}/source-grant',endpoint('source_grant'),methods=['POST']),
        Route('/api/v1/worker/jobs/{job_id}/metadata',endpoint('metadata'),methods=['POST']),
        Route('/api/v1/worker/jobs/{job_id}/source',endpoint('source'),methods=['GET','HEAD']),
        Route('/api/v1/worker/jobs/{job_id}/result-sets',endpoint('create_result'),methods=['POST']),
        Route('/api/v1/worker/jobs/{job_id}/fail',endpoint('fail'),methods=['POST']),
        Route('/api/v1/worker/result-sets/{result_id}/batches/{batch_id}',endpoint('batch'),methods=['PUT']),
        Route('/api/v1/worker/result-sets/{result_id}/artifacts',endpoint('begin_artifact'),methods=['POST']),
        Route('/api/v1/worker/artifacts/{artifact_id}/parts/{part_no}',endpoint('artifact_part'),methods=['PUT']),
        Route('/api/v1/worker/artifacts/{artifact_id}/status',endpoint('artifact_status'),methods=['POST']),
        Route('/api/v1/worker/artifacts/{artifact_id}/complete',endpoint('complete_artifact'),methods=['POST']),
        Route('/api/v1/worker/result-sets/{result_id}/complete',endpoint('complete'),methods=['POST']),
    ]
