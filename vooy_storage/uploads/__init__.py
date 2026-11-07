"""Resumable browser uploads with session-scoped grants."""
import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import hmac
import json
import mimetypes
import os
from pathlib import Path
import secrets
import threading
import uuid
import zipfile

from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route
from vooy_storage.repository import _normalize_tags

CHUNK = 8 * 1024 * 1024
MAX_SIZE = 10 * 1024 * 1024 * 1024


def now():
    return datetime.now(timezone.utc)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def file_type(path, filename):
    with open(path, 'rb') as f:
        head = f.read(512)
    if head.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if head.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if head.startswith((b'GIF87a', b'GIF89a')):
        return 'image/gif'
    if head.startswith(b'RIFF') and head[8:12] == b'WEBP':
        return 'image/webp'
    if head.startswith(b'%PDF-'):
        return 'application/pdf'
    if head.startswith(b'PK\x03\x04') and Path(filename).suffix.lower() == '.docx':
        try:
            with zipfile.ZipFile(path) as archive:
                if 'word/document.xml' in archive.namelist() and '[Content_Types].xml' in archive.namelist():
                    return 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
        except (OSError, zipfile.BadZipFile):
            pass
        raise ValueError('올바른 DOCX 문서가 아닙니다.')
    if head[4:8] == b'ftyp':
        if head[8:12] in (b'heic', b'heix', b'mif1'):
            return 'image/heic'
        if head[8:12] == b'qt  ':
            return 'video/quicktime'
        if Path(filename).suffix.lower() == '.m4a':
            return 'audio/mp4'
        return 'video/mp4'
    if head.startswith(b'\x1aE\xdf\xa3'):
        return 'video/webm'
    if head.startswith(b'RIFF') and head[8:12] == b'WAVE':
        return 'audio/wav'
    if head.startswith(b'ID3') or (head[:1] == b'\xff' and len(head) > 1 and head[1] & 0xe0 == 0xe0):
        return 'audio/mpeg'
    ext = Path(filename).suffix.lower()
    if ext in ('.txt', '.md', '.json'):
        with open(path, 'r', encoding='utf-8') as f:
            while f.read(1024 * 1024):
                pass
        return {'.txt': 'text/plain', '.md': 'text/markdown', '.json': 'application/json'}[ext]
    raise ValueError('지원하지 않는 파일 형식입니다.')


class UploadManager:
    def __init__(self, repository, media_root, public_base_url):
        self.repository = repository
        self.root = Path(media_root) / 'staging'
        self.root.mkdir(parents=True, exist_ok=True)
        self.base = public_base_url.rstrip('/')
        self.web = Path(__file__).resolve().parent.parent / 'web'
        self.active = set()
        self.active_lock = threading.Lock()
        for item in self.root.iterdir():
            if item.is_dir():
                try:
                    state = self._read(item.name)
                    if state['state'] == 'finalizing':
                        self._start(item.name)
                except (ValueError, OSError, KeyError):
                    pass

    def _path(self, uid):
        if str(uuid.UUID(uid)) != uid:
            raise ValueError('invalid upload id')
        return self.root / uid

    @contextmanager
    def _lock(self, uid):
        path = self._path(uid)
        if not path.is_dir():
            raise FileNotFoundError(uid)
        with open(path / '.lock', 'a+b') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield path

    def _read(self, uid):
        return json.loads((self._path(uid) / 'session.json').read_text())

    def _write(self, uid, state):
        target = self._path(uid) / 'session.json'
        temporary = target.with_suffix('.tmp')
        temporary.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
        os.replace(temporary, target)

    def create_upload(self, title, kind='file', asset_id=None, expected_version_id=None,
                      tags=None, source=None):
        if not isinstance(title, str) or not title.strip() or len(title) > 500:
            raise ValueError('제목을 입력해 주세요.')
        if bool(asset_id) != bool(expected_version_id):
            raise ValueError('원본 교체에는 자료 ID와 현재 버전 ID가 필요합니다.')
        if asset_id and (tags is not None or source is not None):
            raise ValueError('원본 교체 시 출처와 태그는 자료 정보 수정으로 변경해 주세요.')
        if source is not None and not isinstance(source, str):
            raise ValueError('출처는 문자열이어야 합니다.')
        tags = _normalize_tags(tags) if tags is not None else None
        if asset_id:
            asset = self.repository.get_asset(asset_id)
            if not asset or asset['version_id'] != expected_version_id:
                raise ValueError('자료를 찾을 수 없거나 원본 버전이 변경되었습니다.')
        uid, token = str(uuid.uuid4()), secrets.token_urlsafe(32)
        folder = self._path(uid)
        folder.mkdir(mode=0o700)
        (folder / 'parts').mkdir()
        state = {'id': uid, 'title': title.strip(), 'kind': kind, 'state': 'created',
                 'expires_at': (now() + timedelta(hours=24)).isoformat(),
                 'grant_expires_at': (now() + timedelta(minutes=15)).isoformat(),
                 'grant_hash': digest(token), 'cookie_hash': None, 'parts': [], 'asset': None,
                 'target_asset_id': asset_id, 'expected_version_id': expected_version_id,
                 'tags': tags, 'source': source}
        self._write(uid, state)
        return {'upload_id': uid, 'upload_url': f'{self.base}/upload/{uid}#token={token}', 'expires_at': state['expires_at']}

    def get_status(self, uid):
        with self._lock(uid):
            state = self._read(uid)
        return self._public(state)

    def issue_link(self, uid):
        token = secrets.token_urlsafe(32)
        with self._lock(uid):
            state = self._read(uid)
            if now() >= datetime.fromisoformat(state['expires_at']) or state['state'] in ('completed', 'failed'):
                raise ValueError('업로드 세션이 종료되었습니다.')
            state['grant_hash'] = digest(token)
            state['grant_expires_at'] = (now() + timedelta(minutes=15)).isoformat()
            self._write(uid, state)
        return {'upload_id': uid, 'upload_url': f'{self.base}/upload/{uid}#token={token}', 'expires_at': state['expires_at']}

    def request_complete(self, uid):
        with self._lock(uid):
            state = self._read(uid)
            if now() >= datetime.fromisoformat(state['expires_at']):
                raise ValueError('업로드 세션이 만료되었습니다.')
            if state['state'] == 'completed':
                return self._public(state)
            if state['state'] == 'uploading' and sum(p['size'] for p in state['parts']) == state['size']:
                state['state'] = 'finalizing'
                self._write(uid, state)
            elif state['state'] != 'finalizing':
                raise ValueError('파일 업로드가 완료되지 않았습니다.')
        self._start(uid)
        return self._public(state)

    @staticmethod
    def _public(state):
        result = {key: state.get(key) for key in ('id', 'title', 'state', 'filename', 'size', 'parts', 'asset', 'error', 'expires_at')}
        if isinstance(result['asset'], dict):
            result['asset'] = {k: v for k, v in result['asset'].items() if k != 'object_key'}
        return result

    def _origin_ok(self, request):
        origin = request.headers.get('origin')
        return origin is None or origin == self.base


    def _authorize(self, request, state):
        token = request.cookies.get('vooy_upload')
        if not token or not state.get('cookie_hash') or not hmac.compare_digest(digest(token), state['cookie_hash']):
            return False
        return now() < datetime.fromisoformat(state['expires_at'])

    async def page(self, request):
        try:
            self._read(request.path_params['uid'])
        except (ValueError, FileNotFoundError):
            return Response(status_code=404)
        return FileResponse(self.web / 'upload.html', headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})

    async def javascript(self, request):
        return FileResponse(self.web / 'upload.js', media_type='application/javascript')

    async def exchange(self, request):
        if not self._origin_ok(request):
            return Response(status_code=403)
        uid = request.path_params['uid']
        try:
            body = await request.json()
            with self._lock(uid):
                state = self._read(uid)
                token = body.get('token', '')
                if not isinstance(token, str) or not state.get('grant_hash') or not hmac.compare_digest(digest(token), state['grant_hash']) or now() >= datetime.fromisoformat(state['grant_expires_at']):
                    return Response(status_code=401)
                cookie = secrets.token_urlsafe(32)
                state['grant_hash'] = None
                state['cookie_hash'] = digest(cookie)
                self._write(uid, state)
            response = Response(status_code=204, headers={'Cache-Control': 'no-store'})
            response.set_cookie('vooy_upload', cookie, httponly=True, secure=self.base.startswith('https://'), samesite='strict', path=f'/api/v1/uploads/{uid}', max_age=86400)
            return response
        except (ValueError, FileNotFoundError, json.JSONDecodeError):
            return Response(status_code=404)

    async def metadata(self, request):
        if not self._origin_ok(request):
            return Response(status_code=403)
        uid = request.path_params['uid']
        try:
            body = await request.json()
            filename = body.get('filename')
            size = body.get('size')
            if not isinstance(filename, str) or not filename or len(filename) > 255 or '/' in filename or '\\' in filename or not isinstance(size, int) or isinstance(size, bool) or size <= 0 or size > MAX_SIZE:
                return JSONResponse({'error': '파일 이름 또는 크기를 확인해 주세요.'}, status_code=400)
            with self._lock(uid):
                state = self._read(uid)
                if not self._authorize(request, state):
                    return Response(status_code=401)
                if state['state'] not in ('created', 'uploading'):
                    return Response(status_code=409)
                if state.get('filename') and (state['filename'] != filename or state['size'] != size):
                    return JSONResponse({'error': '처음 선택한 파일과 다릅니다.'}, status_code=409)
                state.update(filename=filename, size=size, state='uploading')
                self._write(uid, state)
            return JSONResponse(self._public(state))
        except (ValueError, FileNotFoundError, json.JSONDecodeError):
            return Response(status_code=404)

    async def part(self, request):
        if not self._origin_ok(request):
            return Response(status_code=403)
        uid = request.path_params['uid']
        try:
            part_no = int(request.path_params['part_no'])
            with self._lock(uid):
                state = self._read(uid)
                if not self._authorize(request, state):
                    return Response(status_code=401)
                if state['state'] != 'uploading' or part_no < 0 or part_no > len(state['parts']):
                    return Response(status_code=409)
            contents = bytearray()
            async for chunk in request.stream():
                contents.extend(chunk)
                if len(contents) > CHUNK:
                    return Response(status_code=413)
            sha = hashlib.sha256(contents).hexdigest()
            if not hmac.compare_digest(sha, request.headers.get('x-part-sha256', '')):
                return JSONResponse({'error': '파일 조각 해시가 일치하지 않습니다.'}, status_code=422)
            with self._lock(uid) as folder:
                state = self._read(uid)
                if not self._authorize(request, state):
                    return Response(status_code=401)
                if state['state'] != 'uploading':
                    return Response(status_code=409)
                if part_no < len(state['parts']):
                    return Response(status_code=200 if state['parts'][part_no]['sha256'] == sha else 409)
                if part_no != len(state['parts']):
                    return Response(status_code=409)
                expected = min(CHUNK, state['size'] - part_no * CHUNK)
                if expected <= 0 or len(contents) != expected:
                    return Response(status_code=422)
                temp = folder / 'parts' / f'{part_no}.tmp'
                temp.write_bytes(contents)
                os.replace(temp, folder / 'parts' / str(part_no))
                state['parts'].append({'part_no': part_no, 'size': len(contents), 'sha256': sha})
                self._write(uid, state)
            return Response(status_code=200)
        except (ValueError, FileNotFoundError):
            return Response(status_code=404)

    async def status(self, request):
        uid = request.path_params['uid']
        try:
            with self._lock(uid):
                state = self._read(uid)
                if not self._authorize(request, state):
                    return Response(status_code=401)
            return JSONResponse(self._public(state), headers={'Cache-Control': 'no-store'})
        except (ValueError, FileNotFoundError):
            return Response(status_code=404)

    async def complete(self, request):
        if not self._origin_ok(request):
            return Response(status_code=403)
        uid = request.path_params['uid']
        try:
            with self._lock(uid):
                state = self._read(uid)
                if not self._authorize(request, state):
                    return Response(status_code=401)
                if state['state'] == 'completed':
                    return JSONResponse(self._public(state))
                if state['state'] == 'uploading' and sum(p['size'] for p in state['parts']) == state['size']:
                    state['state'] = 'finalizing'
                    self._write(uid, state)
                elif state['state'] != 'finalizing':
                    return Response(status_code=409)
            self._start(uid)
            return JSONResponse(self._public(state), status_code=202)
        except (ValueError, FileNotFoundError):
            return Response(status_code=404)

    def _start(self, uid):
        with self.active_lock:
            if uid in self.active:
                return
            self.active.add(uid)
        threading.Thread(target=self._finalize, args=(uid,), daemon=True).start()

    def _finalize(self, uid):
        try:
            folder = self._path(uid)
            # A separate lock prevents two processes from finalizing the same upload;
            # ordinary status requests can still read session.json while hashing.
            with open(folder / '.finalize.lock', 'a+b') as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return
                with self._lock(uid):
                    state = self._read(uid)
                    if state['state'] != 'finalizing':
                        return
                output = folder / 'assembled'
                sha = hashlib.sha256()
                with output.open('wb') as out:
                    for item in state['parts']:
                        part_hash = hashlib.sha256()
                        part_size = 0
                        with (folder / 'parts' / str(item['part_no'])).open('rb') as part:
                            while data := part.read(1024 * 1024):
                                sha.update(data)
                                part_hash.update(data)
                                part_size += len(data)
                                out.write(data)
                        if part_size != item['size'] or not hmac.compare_digest(part_hash.hexdigest(), item['sha256']):
                            raise ValueError('파일 조각의 내용이 일치하지 않습니다.')
                media_type = file_type(output, state['filename'])
                if state.get('target_asset_id'):
                    asset = self.repository.replace_file(state['target_asset_id'], state['expected_version_id'], state['filename'], media_type, output, state['size'], sha.hexdigest(), request_id=uid)
                else:
                    kwargs = {'asset_id': uid}
                    if state.get('tags') is not None or state.get('source') is not None:
                        kwargs.update(tags=state.get('tags'), source=state.get('source'))
                    asset = self.repository.save_file(state['title'], state['filename'], media_type, output, state['size'], sha.hexdigest(), **kwargs)
                with self._lock(uid):
                    state = self._read(uid)
                    state.update(state='completed', asset=asset)
                    self._write(uid, state)
                output.unlink(missing_ok=True)
                for item in state['parts']:
                    (folder / 'parts' / str(item['part_no'])).unlink(missing_ok=True)
        except Exception as exc:
            try:
                with self._lock(uid):
                    state = self._read(uid)
                    message = str(exc)[:200] if isinstance(exc, (ValueError, UnicodeDecodeError)) else '파일 저장 중 오류가 발생했습니다.'
                    state.update(state='failed', error=message)
                    self._write(uid, state)
            except Exception:
                pass
        finally:
            with self.active_lock:
                self.active.discard(uid)

    def routes(self):
        return [Route('/upload/{uid}', self.page), Route('/upload-static/upload.js', self.javascript),
                Route('/api/v1/uploads/{uid}/exchange', self.exchange, methods=['POST']),
                Route('/api/v1/uploads/{uid}/file', self.metadata, methods=['PUT']),
                Route('/api/v1/uploads/{uid}/parts/{part_no}', self.part, methods=['PUT']),
                Route('/api/v1/uploads/{uid}/complete', self.complete, methods=['POST']),
                Route('/api/v1/uploads/{uid}', self.status, methods=['GET'])]
