"""Asynchronous public-URL import with per-redirect DNS pinning."""
from __future__ import annotations

import fcntl
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import socket
import ssl
import threading
import time
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit
import uuid

from vooy_storage.uploads import MAX_SIZE, file_type


def validate_url(url):
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('공개 HTTP(S) 주소가 필요합니다.')
    try:
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        if port not in (80, 443):
            raise ValueError('지원하지 않는 포트입니다.')
        try:
            literal = ipaddress.ip_address(parsed.hostname)
            if not literal.is_global:
                raise ValueError('공개 주소가 아닙니다.')
        except ValueError as exc:
            if str(exc) == '공개 주소가 아닙니다.':
                raise
        entries = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
        addresses = list(dict.fromkeys(entry[4][0] for entry in entries))
        if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
            raise ValueError('공개 주소가 아닙니다.')
        return parsed, addresses[0]
    except (socket.gaierror, OverflowError) as exc:
        raise ValueError('주소를 확인할 수 없습니다.') from exc


class PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host, port, ip):
        super().__init__(host, port, timeout=30)
        self.ip = ip

    def connect(self):
        self.sock = socket.create_connection((self.ip, self.port), self.timeout)


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, port, ip):
        super().__init__(host, port, timeout=30, context=ssl.create_default_context())
        self.ip = ip

    def connect(self):
        sock = socket.create_connection((self.ip, self.port), self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except Exception:
            sock.close()
            raise


class ImportManager:
    def __init__(self, repository, media_root):
        self.repo = repository
        self.root = Path(media_root) / 'imports'
        self.root.mkdir(parents=True, exist_ok=True)
        self.active = set()
        self.lock = threading.Lock()
        for folder in self.root.iterdir():
            if folder.is_dir():
                try:
                    state = self._read(folder.name)
                    if state['state'] in ('queued', 'running'):
                        self._start(folder.name)
                except (OSError, ValueError, KeyError):
                    continue

    def _folder(self, job_id):
        if str(uuid.UUID(job_id)) != job_id:
            raise ValueError('작업 ID가 올바르지 않습니다.')
        return self.root / job_id

    def _read(self, job_id):
        return json.loads((self._folder(job_id) / 'status.json').read_text(encoding='utf-8'))

    def _write(self, job_id, state):
        path = self._folder(job_id) / 'status.json'
        temp = path.with_suffix('.tmp')
        temp.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
        os.chmod(temp, 0o600)
        os.replace(temp, path)

    def create(self, title, url):
        if not isinstance(title, str) or not title.strip() or len(title) > 500:
            raise ValueError('제목을 입력해 주세요.')
        validate_url(url)
        job_id = str(uuid.uuid4())
        self._folder(job_id).mkdir(mode=0o700)
        self._write(job_id, {'id': job_id, 'title': title.strip(), 'url': url, 'state': 'queued'})
        self._start(job_id)
        return {'job_id': job_id, 'state': 'queued'}

    def status(self, job_id):
        state = self._read(job_id)
        return {key: state[key] for key in ('id', 'title', 'state', 'asset_id', 'error') if key in state}

    def _start(self, job_id):
        with self.lock:
            if job_id in self.active:
                return
            self.active.add(job_id)
        threading.Thread(target=self._run, args=(job_id,), daemon=True).start()

    def _run(self, job_id):
        folder = self._folder(job_id)
        try:
            with (folder / '.lock').open('a+b') as lockfile:
                try:
                    fcntl.flock(lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return
                state = self._read(job_id)
                if state['state'] not in ('queued', 'running'):
                    return
                state['state'] = 'running'
                self._write(job_id, state)
                url = state['url']
                deadline = time.monotonic() + 7200
                for hop in range(4):
                    parsed, ip = validate_url(url)
                    port = parsed.port or (443 if parsed.scheme == 'https' else 80)
                    connection = (PinnedHTTPS if parsed.scheme == 'https' else PinnedHTTP)(parsed.hostname, port, ip)
                    try:
                        target = urlunsplit(('', '', parsed.path or '/', parsed.query, ''))
                        connection.request('GET', target, headers={'User-Agent': 'VooyStorage/1.0', 'Accept': '*/*', 'Accept-Encoding': 'identity'})
                        response = connection.getresponse()
                        if response.status in (301, 302, 303, 307, 308):
                            location = response.getheader('Location')
                            if not location or hop == 3:
                                raise ValueError('리다이렉트가 너무 많습니다.')
                            url = urljoin(url, location)
                            continue
                        if response.status != 200:
                            raise ValueError(f'파일 서버가 HTTP {response.status}을 반환했습니다.')
                        length = response.getheader('Content-Length')
                        if length is not None and (not length.isdigit() or int(length) > MAX_SIZE):
                            raise ValueError('파일 크기가 허용 범위를 벗어났습니다.')
                        output = folder / 'download'
                        digest = hashlib.sha256()
                        size = 0
                        with output.open('wb') as stream:
                            while data := response.read(1024 * 1024):
                                size += len(data)
                                if size > MAX_SIZE or time.monotonic() > deadline:
                                    raise ValueError('파일 크기 또는 전송 시간을 초과했습니다.')
                                digest.update(data)
                                stream.write(data)
                        if size == 0 or (length is not None and size != int(length)):
                            raise ValueError('다운로드한 파일의 크기가 일치하지 않습니다.')
                        filename = Path(unquote(parsed.path)).name or 'download'
                        kind = file_type(output, filename)
                        asset = self.repo.save_file(state['title'], filename, kind, output, size, digest.hexdigest(), asset_id=job_id)
                        source = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, '', ''))
                        self.repo.update_asset(asset['id'], source=source)
                        state.update(state='completed', asset_id=asset['id'])
                        state.pop('url', None)
                        self._write(job_id, state)
                        output.unlink(missing_ok=True)
                        return
                    finally:
                        connection.close()
        except Exception as exc:
            state = self._read(job_id)
            state.update(state='failed', error=str(exc)[:200] if isinstance(exc, ValueError) else '파일 가져오기에 실패했습니다.')
            state.pop('url', None)
            self._write(job_id, state)
            (folder / 'download').unlink(missing_ok=True)
        finally:
            with self.lock:
                self.active.discard(job_id)
