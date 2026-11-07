"""Create searchable audio intervals and document pages using OpenRouter."""
from __future__ import annotations

import base64
from contextlib import contextmanager
import io
import math
from pathlib import Path
import subprocess
import tempfile
import zipfile
from xml.etree import ElementTree


MAX_AUDIO_SECONDS = 120
DOCX_MIME = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
MAX_DOCX_XML_BYTES = 16 * 1024 * 1024


@contextmanager
def _open_binary(raw):
    """Read source bytes or a file incrementally without copying large files."""
    if isinstance(raw, bytes):
        if not raw:
            raise ValueError('자료 내용이 비어 있습니다.')
        with io.BytesIO(raw) as stream:
            yield stream
    elif isinstance(raw, (str, Path)):
        with Path(raw).open('rb') as stream:
            yield stream
    else:
        raise ValueError('자료는 파일 경로 또는 바이트여야 합니다.')


class MultimediaEmbeddings:
    """Extend the configured OpenRouter space with file/audio inputs."""

    def __init__(self, embeddings):
        self.embeddings = embeddings

    def embed_audio(self, wav_bytes):
        if not wav_bytes or not wav_bytes.startswith(b'RIFF') or wav_bytes[8:12] != b'WAVE':
            raise ValueError('유효한 WAV 음성이 필요합니다.')
        uri = 'data:audio/wav;base64,' + base64.b64encode(wav_bytes).decode('ascii')
        return self.embeddings._embed([{'content': [{'type': 'input_audio',
            'input_audio': {'data': uri, 'format': 'wav'}}]}])

    def embed_pdf_page(self, pdf_bytes):
        if not pdf_bytes or not pdf_bytes.startswith(b'%PDF-'):
            raise ValueError('유효한 PDF 페이지가 필요합니다.')
        uri = 'data:application/pdf;base64,' + base64.b64encode(pdf_bytes).decode('ascii')
        return self.embeddings._embed([{'content': [{'type': 'input_file',
            'input_file': {'data': uri, 'format': 'pdf'}}]}])

    def embed_document(self, text, title=None):
        return self.embeddings.embed_document(text, title=title)


def _segment(external_id, kind, text, locator, vector):
    return {'external_id': external_id, 'kind': kind, 'text': text, 'locator': locator,
            'vectors': [{'ordinal': 0, 'values': vector}]}


def _docx_text(raw):
    """Extract Word body paragraphs. Offsets refer to this extracted text."""
    try:
        with _open_binary(raw) as source, zipfile.ZipFile(source) as archive:
            entry = archive.getinfo('word/document.xml')
            if entry.file_size > MAX_DOCX_XML_BYTES:
                raise ValueError('문서 본문이 처리 크기를 초과했습니다.')
            document = ElementTree.fromstring(archive.read(entry))
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError, OSError) as exc:
        raise ValueError('Word 문서 본문을 읽을 수 없습니다.') from exc
    ns = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
    paragraphs = []
    for paragraph in document.iter(ns + 'p'):
        pieces = []
        for node in paragraph.iter():
            if node.tag == ns + 't' and node.text:
                pieces.append(node.text)
            elif node.tag == ns + 'tab':
                pieces.append('\t')
            elif node.tag in (ns + 'br', ns + 'cr'):
                pieces.append('\n')
        paragraphs.append(''.join(pieces))
    return '\n'.join(paragraphs)


def iter_document_segments(raw, mime_type, title, embeddings, chunk_chars=2000):
    """Yield one vector per PDF page or bounded UTF-8 text chunk."""
    mime_type = (mime_type or '').lower().split(';', 1)[0].strip()
    if mime_type == 'application/pdf':
        from pypdf import PdfReader, PdfWriter
        from pypdf.errors import PdfReadError
        try:
            with _open_binary(raw) as source:
                reader = PdfReader(source, strict=True)
                if reader.is_encrypted:
                    raise ValueError('암호로 보호된 PDF는 색인할 수 없습니다.')
                pages = reader.pages
                if not pages:
                    raise ValueError('PDF에 페이지가 없습니다.')
                for index, page in enumerate(pages, 1):
                    writer = PdfWriter()
                    writer.add_page(page)
                    stream = io.BytesIO()
                    writer.write(stream)
                    page_bytes = stream.getvalue()
                    # A PDF image page also works when no extractable text exists.
                    try:
                        content = page.extract_text() or None
                    except Exception:
                        content = None
                    if content and len(content.encode('utf-8')) > 256 * 1024:
                        content = None
                    yield _segment(f'page-{index}', 'document', content, {'page': index},
                                   embeddings.embed_pdf_page(page_bytes))
        except (PdfReadError, EOFError, OSError) as exc:
            raise ValueError('PDF를 읽을 수 없습니다.') from exc
        return
    if mime_type not in ('text/plain', 'text/markdown', 'application/json', DOCX_MIME):
        raise ValueError('지원하지 않는 문서 형식입니다.')
    if isinstance(chunk_chars, bool) or not isinstance(chunk_chars, int) or not 1 <= chunk_chars <= 8192:
        raise ValueError('문서 구간 크기가 올바르지 않습니다.')
    try:
        content_bytes = _docx_text(raw).encode('utf-8') if mime_type == DOCX_MIME else raw
        with _open_binary(content_bytes) as binary:
            with io.TextIOWrapper(binary, encoding='utf-8-sig', newline='') as stream:
                start = 0
                emitted = False
                while content := stream.read(chunk_chars):
                    end = start + len(content)
                    if content.strip():
                        emitted = True
                        locator = {'char_start': start, 'char_end': end}
                        if mime_type == DOCX_MIME:
                            locator['text_source'] = 'docx/word/document.xml'
                        yield _segment(f'text-{start}', 'document', content, locator,
                                       embeddings.embed_document(content, title=title))
                    start = end
                if not emitted:
                    raise ValueError('텍스트 문서가 비어 있습니다.')
    except UnicodeDecodeError as exc:
        raise ValueError('텍스트 문서는 UTF-8이어야 합니다.') from exc


def iter_audio_segments(raw, embeddings, chunk_seconds=MAX_AUDIO_SECONDS):
    """Normalize each audio interval to 16 kHz WAV before direct embedding."""
    if not isinstance(raw, (bytes, str, Path)) or not raw:
        raise ValueError('음성 내용이 비어 있습니다.')
    if isinstance(chunk_seconds, bool) or not isinstance(chunk_seconds, int) or not 1 <= chunk_seconds <= MAX_AUDIO_SECONDS:
        raise ValueError('음성 구간 길이가 올바르지 않습니다.')
    with tempfile.TemporaryDirectory(prefix='vooy-audio-') as directory:
        if isinstance(raw, bytes):
            source = Path(directory) / 'source'
            source.write_bytes(raw)
        else:
            source = Path(raw)
        try:
            probe = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'a:0',
                                    '-show_entries', 'format=duration', '-of',
                                    'default=noprint_wrappers=1:nokey=1', str(source)],
                                   check=True, capture_output=True, timeout=30)
            duration = float(probe.stdout.decode('ascii').strip())
        except (subprocess.SubprocessError, OSError, UnicodeError, ValueError) as exc:
            raise ValueError('음성 파일의 길이를 확인할 수 없습니다.') from exc
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError('음성 길이가 지원 범위를 벗어났습니다.')
        total_ms = max(1, round(duration * 1000))
        step_ms = chunk_seconds * 1000
        for start_ms in range(0, total_ms, step_ms):
            end_ms = min(total_ms, start_ms + step_ms)
            try:
                result = subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-i', str(source),
                    '-ss', f'{start_ms / 1000:.3f}', '-t', f'{(end_ms - start_ms) / 1000:.3f}',
                    '-map', '0:a:0', '-ac', '1', '-ar', '16000', '-acodec', 'pcm_s16le', '-f', 'wav', 'pipe:1'],
                    check=True, capture_output=True, timeout=150)
            except (subprocess.SubprocessError, OSError) as exc:
                raise ValueError('음성 구간을 변환할 수 없습니다.') from exc
            vector = embeddings.embed_audio(result.stdout)
            yield _segment(f'audio-{start_ms}-{end_ms}', 'audio', None,
                           {'start_ms': start_ms, 'end_ms': end_ms}, vector)
