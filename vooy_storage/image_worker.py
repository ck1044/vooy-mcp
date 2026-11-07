"""Background worker for image and text assets using the RAG worker contract."""
import base64
from datetime import datetime
import hashlib
import io
import logging
import os
import re
import tempfile
import threading
import time
from pathlib import Path

import httpx2
from pillow_heif import register_heif_opener

from vooy_storage.openrouter import OpenRouterEmbeddings

LOG = logging.getLogger(__name__)
register_heif_opener()
MAX_SOURCE_BYTES = 50 * 1024 * 1024
PREPROCESS_REVISION = 'image-jpeg-v1'


class WorkerError(Exception):
    def __init__(self, code, retryable=False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


def extract_metadata(content):
    """Read camera facts while keeping local capture time distinct from its offset."""
    from PIL import Image
    result = {'captured_at_local': None, 'utc_offset_minutes': None, 'latitude': None, 'longitude': None}
    try:
        with Image.open(io.BytesIO(content)) as image:
            exif = image.getexif()
            details = exif.get_ifd(0x8769)
            date_value = details.get(0x9003) or exif.get(0x9003)
            offset = details.get(0x9011) or exif.get(0x9011)
            if isinstance(date_value, str):
                try:
                    result['captured_at_local'] = datetime.strptime(date_value.strip('\x00 '), '%Y:%m:%d %H:%M:%S').isoformat()
                except ValueError:
                    pass
            if isinstance(offset, str):
                match = re.fullmatch(r'([+-])(\d{2}):(\d{2})', offset.strip('\x00 '))
                if match and int(match[3]) < 60:
                    minutes = (int(match[2]) * 60 + int(match[3])) * (1 if match[1] == '+' else -1)
                    if -840 <= minutes <= 840:
                        result['utc_offset_minutes'] = minutes
            gps = exif.get_ifd(0x8825)
            def coordinate(value, reference, limit, allowed):
                if not isinstance(value, (tuple, list)) or len(value) != 3:
                    return None
                degrees, minutes, seconds = (float(part) for part in value)
                if not (0 <= degrees <= limit and 0 <= minutes < 60 and 0 <= seconds < 60):
                    return None
                ref = str(reference).strip('\x00 ').upper()
                if ref not in allowed:
                    return None
                number = (degrees + minutes / 60 + seconds / 3600) * (-1 if ref in ('S', 'W') else 1)
                return number if abs(number) <= limit else None
            lat = coordinate(gps.get(2), gps.get(1), 90, ('N', 'S'))
            lon = coordinate(gps.get(4), gps.get(3), 180, ('E', 'W'))
            if lat is not None and lon is not None:
                result['latitude'], result['longitude'] = lat, lon
    except (OSError, ValueError, TypeError, ZeroDivisionError):
        pass
    return result


def prepare_image(content):
    """Apply orientation, flatten alpha, and produce inference and preview JPEGs."""
    from PIL import Image, ImageOps, UnidentifiedImageError
    try:
        with Image.open(io.BytesIO(content)) as source:
            source.seek(0)
            if source.width * source.height > 100_000_000:
                raise WorkerError('image_too_large')
            image = ImageOps.exif_transpose(source)
            if image.mode in ('RGBA', 'LA') or 'transparency' in image.info:
                rgba = image.convert('RGBA')
                image = Image.new('RGB', rgba.size, 'white')
                image.paste(rgba, mask=rgba.getchannel('A'))
            else:
                image = image.convert('RGB')
            image.thumbnail((2048, 2048))
            out = io.BytesIO()
            image.save(out, format='JPEG', quality=88, optimize=True)
            image.thumbnail((768, 768))
            thumbnail = io.BytesIO()
            image.save(thumbnail, format='JPEG', quality=82, optimize=True)
            return out.getvalue(), thumbnail.getvalue()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise WorkerError('unsupported_image') from exc


class ImageWorker:
    def __init__(self, base_url, worker_token, space_id, embeddings, pipeline_revision='openrouter-image-v1', client=None):
        self.base_url = base_url.rstrip('/') + '/api/v1/worker'
        self.space_id = space_id
        self.pipeline_revision = pipeline_revision
        self.embeddings = embeddings
        self.client = client or httpx2.Client(timeout=60, follow_redirects=False, trust_env=False)
        self.headers = {'Authorization': 'Bearer ' + worker_token}

    def _request(self, method, path, **kwargs):
        try:
            res = self.client.request(method, self.base_url + path, headers=self.headers, **kwargs)
            if res.status_code == 204:
                return None
            res.raise_for_status()
            return res
        except httpx2.HTTPStatusError as exc:
            status = exc.response.status_code
            raise WorkerError(f'worker_http_{status}', status in (408, 429, 500, 502, 503, 504)) from exc
        except httpx2.RequestError as exc:
            raise WorkerError('worker_unreachable', True) from exc

    def _json(self, method, path, data):
        response = self._request(method, path, json=data)
        return response.json() if response is not None else None

    def _source(self, job_id, epoch):
        try:
            with self.client.stream('GET', self.base_url + f'/jobs/{job_id}/source?lease_epoch={epoch}',
                                    headers=self.headers) as response:
                response.raise_for_status()
                chunks = bytearray()
                for chunk in response.iter_bytes():
                    chunks.extend(chunk)
                    if len(chunks) > MAX_SOURCE_BYTES:
                        raise WorkerError('source_too_large')
                return bytes(chunks)
        except httpx2.HTTPStatusError as exc:
            status = exc.response.status_code
            raise WorkerError(f'worker_http_{status}', status in (408, 429, 500, 502, 503, 504)) from exc
        except httpx2.RequestError as exc:
            raise WorkerError('worker_unreachable', True) from exc

    def _source_file(self, job_id, epoch, filename):
        try:
            with self.client.stream('GET', self.base_url + f'/jobs/{job_id}/source?lease_epoch={epoch}',
                                    headers=self.headers) as response:
                response.raise_for_status()
                size = 0
                with open(filename, 'wb') as output:
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > 10 * 1024**3:
                            raise WorkerError('source_too_large')
                        output.write(chunk)
        except httpx2.HTTPStatusError as exc:
            status = exc.response.status_code
            raise WorkerError(f'worker_http_{status}', status in (408,429,500,502,503,504)) from exc
        except httpx2.RequestError as exc:
            raise WorkerError('worker_unreachable', True) from exc

    def _upload_artifact(self, result_id, epoch, kind, filename, mime_type, data, revision):
        digest = hashlib.sha256(data).hexdigest()
        artifact = self._json('POST', f'/result-sets/{result_id}/artifacts', {
            'lease_epoch': epoch, 'kind': kind, 'filename': filename, 'mime_type': mime_type,
            'size_bytes': len(data), 'sha256': digest, 'extractor_revision': revision})
        artifact_id = artifact['id']
        for part_no, start in enumerate(range(0, len(data), 8 * 1024 * 1024)):
            chunk = data[start:start + 8 * 1024 * 1024]
            headers = {**self.headers, 'X-Lease-Epoch': str(epoch), 'X-Part-SHA256': hashlib.sha256(chunk).hexdigest()}
            try:
                response = self.client.put(self.base_url + f'/artifacts/{artifact_id}/parts/{part_no}',
                                           headers=headers, content=chunk)
                response.raise_for_status()
            except httpx2.HTTPError as exc:
                raise WorkerError('artifact_upload_failed', True) from exc
        self._json('POST', f'/artifacts/{artifact_id}/complete', {'lease_epoch': epoch})
        return artifact_id

    def claim(self):
        return self._json('POST', '/jobs/claim', {'pipeline_revisions': [self.pipeline_revision], 'space_ids': [self.space_id]})

    def run_once(self):
        job = self.claim()
        if job is None:
            return False
        job_id, epoch = job['id'], job['lease_epoch']
        stop = threading.Event()
        heartbeat_errors = []

        def heartbeat():
            while not stop.wait(50):
                try:
                    self._json('POST', f'/jobs/{job_id}/heartbeat', {'lease_epoch': epoch})
                except Exception as exc:
                    heartbeat_errors.append(exc)
                    return

        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        try:
            grant = self._json('POST', f'/jobs/{job_id}/source-grant', {'lease_epoch': epoch})
            kind = grant.get('kind')
            result = self._json('POST', f'/jobs/{job_id}/result-sets', {'lease_epoch': epoch})
            result_id = result['id']
            artifact_ids = []
            segments, batches = [], []
            segment_count = 0

            def flush():
                if not segments:
                    return
                batch_id = 'primary' if not batches else f'batch-{len(batches):06d}'
                self._json('PUT', f'/result-sets/{result_id}/batches/{batch_id}', {
                    'lease_epoch': epoch, 'source_version_id': job['asset_version_id'],
                    'space_id': job['space_id'], 'segments': list(segments)})
                batches.append(batch_id)
                segments.clear()

            def add_segment(item):
                nonlocal segment_count
                segments.append(item)
                segment_count += 1
                if len(segments) >= 16:
                    flush()
            preview = None
            if kind == 'image':
                # Use our own trusted worker origin; never forward credentials to grant URLs.
                raw = self._source(job_id, epoch)
                facts = extract_metadata(raw)
                self._json('POST', f'/jobs/{job_id}/metadata', {'lease_epoch': epoch, **facts})
                image, preview = prepare_image(raw)
                uri = 'data:image/jpeg;base64,' + base64.b64encode(image).decode('ascii')
                vector = self.embeddings.embed_image(uri)
                segment = {'external_id': 'image-full', 'kind': 'image', 'text': None,
                           'locator': {'bbox': [0, 0, 1, 1], 'input_sha256': hashlib.sha256(image).hexdigest(),
                                       'preprocessing': PREPROCESS_REVISION},
                           'vectors': [{'ordinal': 0, 'values': vector}]}
            elif kind == 'text':
                content = grant.get('text_body') or ''
                if not content.strip():
                    raise WorkerError('empty_text')
                vector = self.embeddings.embed_document(content, title=grant.get('title'))
                segment = {'external_id': 'text-full', 'kind': 'text', 'text': content,
                           'locator': {'char_start': 0, 'char_end': len(content)},
                           'vectors': [{'ordinal': 0, 'values': vector}]}
            elif kind in ('video', 'audio', 'document'):
                from vooy_storage.audio_document import MultimediaEmbeddings, iter_audio_segments, iter_document_segments
                from vooy_storage.video_ingestion import iter_video_frames, video_segment, video_frame_data_url, has_audio_stream
                with tempfile.TemporaryDirectory(prefix='vooy-media-') as directory:
                    source = str(Path(directory) / 'source')
                    self._source_file(job_id, epoch, source)
                    if kind == 'video':
                        for frame in iter_video_frames(source):
                            vector = self.embeddings.embed_image(video_frame_data_url(frame))
                            aid = self._upload_artifact(result_id, epoch, 'frame',
                                f"frame-{frame['index']:04d}.jpg", 'image/jpeg', frame['jpeg'], 'video-frame-v1')
                            artifact_ids.append(aid)
                            add_segment(video_segment(frame, vector, aid))
                        if has_audio_stream(source):
                            for item in iter_audio_segments(source, MultimediaEmbeddings(self.embeddings)):
                                item['external_id'] = 'video-' + item['external_id']
                                item['kind'] = 'video'
                                item['locator']['source'] = 'video_audio'
                                add_segment(item)
                    else:
                        media = MultimediaEmbeddings(self.embeddings)
                        iterator = (iter_audio_segments(source, media) if kind == 'audio' else
                                    iter_document_segments(source, grant.get('mime_type'), grant.get('title'), media))
                        for item in iterator:
                            add_segment(item)
                segment = None
            else:
                raise WorkerError('unsupported_media')
            if heartbeat_errors:
                raise WorkerError('heartbeat_failed', True)
            if segment is not None:
                add_segment(segment)
            if not segment_count:
                raise WorkerError('empty_media')
            if preview is not None:
                artifact_ids.append(self._upload_artifact(result_id, epoch, 'thumbnail', 'thumbnail.jpg',
                                    'image/jpeg', preview, PREPROCESS_REVISION))
            flush()
            self._json('POST', f'/result-sets/{result_id}/complete', {'lease_epoch': epoch, 'manifest': {
                'batches': batches, 'segment_count': segment_count, 'vector_count': segment_count,
                'artifact_ids': artifact_ids, 'artifact_count': len(artifact_ids)}})
        except Exception as exc:
            if isinstance(exc, WorkerError):
                error = exc
            elif isinstance(exc, httpx2.HTTPStatusError):
                status = exc.response.status_code
                error = WorkerError(f'embedding_http_{status}', status in (408, 429, 500, 502, 503, 504, 529))
            elif isinstance(exc, httpx2.RequestError):
                error = WorkerError('embedding_unreachable', True)
            else:
                error = WorkerError('embedding_invalid_response')
            try:
                self._json('POST', f'/jobs/{job_id}/fail', {'lease_epoch': epoch, 'error_code': error.code, 'retryable': error.retryable})
            except WorkerError:
                LOG.warning('Could not report worker failure; lease will expire')
            LOG.warning('Job failed: %s', error.code)
        finally:
            stop.set()
            thread.join(timeout=5)
        return True


def main():
    logging.basicConfig(level=logging.INFO)
    logging.getLogger('httpx2').setLevel(logging.WARNING)
    token = Path(os.environ['VOOY_RAG_WORKER_TOKEN_FILE']).read_text().strip()
    api_key = Path(os.environ['VOOY_OPENROUTER_API_KEY_FILE']).read_text().strip()
    model = os.getenv('VOOY_EMBEDDING_MODEL', 'google/gemini-embedding-2')
    dimensions = int(os.getenv('VOOY_EMBEDDING_DIMENSIONS', '1536'))
    embeddings = OpenRouterEmbeddings(api_key, model=model, dimensions=dimensions,
                                     provider=os.getenv('VOOY_EMBEDDING_PROVIDER') or None)
    worker = ImageWorker(os.environ['VOOY_WORKER_BASE_URL'], token, os.environ['VOOY_EMBEDDING_SPACE_ID'], embeddings,
                         os.getenv('VOOY_RAG_PIPELINE_REVISION', 'openrouter-image-v1'))
    while True:
        try:
            if not worker.run_once():
                time.sleep(3)
        except WorkerError as exc:
            LOG.warning('Worker polling failed: %s', exc.code)
            time.sleep(10)


if __name__ == '__main__':
    main()
