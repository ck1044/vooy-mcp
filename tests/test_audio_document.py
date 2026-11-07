import base64
import io
import struct
import tempfile
import unittest
import tempfile
import zipfile
from pathlib import Path

from vooy_storage.uploads import file_type


class DocumentUploadTypeTests(unittest.TestCase):
    def test_docx_signature_and_structure_required(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'x.docx'
            with zipfile.ZipFile(path, 'w') as archive:
                archive.writestr('[Content_Types].xml', '<Types/>')
                archive.writestr('word/document.xml', '<document/>')
            self.assertEqual(file_type(path, path.name),
                'application/vnd.openxmlformats-officedocument.wordprocessingml.document')
            with zipfile.ZipFile(path, 'w') as archive:
                archive.writestr('other.txt', 'test')
            with self.assertRaises(ValueError):
                file_type(path, path.name)
import wave
import zipfile
from pathlib import Path

from pypdf import PdfWriter

from vooy_storage.audio_document import DOCX_MIME, MultimediaEmbeddings, iter_audio_segments, iter_document_segments


class CaptureAPI:
    def __init__(self):
        self.calls = []

    def _embed(self, value):
        self.calls.append(value)
        return [0.6, 0.8]

    def embed_document(self, text, title=None):
        self.calls.append((text, title))
        return [0.6, 0.8]


class AudioDocumentTests(unittest.TestCase):
    def test_pdf_embeds_each_page_and_preserves_page_location(self):
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        writer.add_blank_page(width=100, height=100)
        stream = io.BytesIO()
        writer.write(stream)
        api = CaptureAPI()
        result = list(iter_document_segments(stream.getvalue(), 'application/pdf', '문서', MultimediaEmbeddings(api)))
        self.assertEqual([x['locator']['page'] for x in result], [1, 2])
        self.assertTrue(all(x['kind'] == 'document' for x in result))
        self.assertEqual(len(api.calls), 2)
        for call in api.calls:
            item = call[0]['content'][0]
            self.assertEqual(item['type'], 'input_file')
            self.assertTrue(base64.b64decode(item['input_file']['data'].split(',', 1)[1]).startswith(b'%PDF-'))

    def test_text_documents_are_split_and_offsets_remain_exact(self):
        raw = ('바닷가 여행 기록입니다. ' * 500).encode()
        api = CaptureAPI()
        items = list(iter_document_segments(raw, 'text/plain', '기록', MultimediaEmbeddings(api), chunk_chars=1000))
        self.assertGreater(len(items), 1)
        text = raw.decode()
        self.assertEqual(''.join(item['text'] for item in items), text)
        for item in items:
            self.assertEqual(text[item['locator']['char_start']:item['locator']['char_end']], item['text'])

    def test_wav_audio_is_embedded_with_timed_locator(self):
        stream = io.BytesIO()
        with wave.open(stream, 'wb') as f:
            f.setnchannels(1)
            f.setsampwidth(2)
            f.setframerate(16000)
            f.writeframes(struct.pack('<h', 0) * 16000)
        api = CaptureAPI()
        items = list(iter_audio_segments(stream.getvalue(), MultimediaEmbeddings(api)))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['kind'], 'audio')
        self.assertEqual(items[0]['locator']['start_ms'], 0)
        self.assertEqual(items[0]['locator']['end_ms'], 1000)
        self.assertEqual(api.calls[0][0]['content'][0]['type'], 'input_audio')

    def test_word_document_extracts_text_and_records_its_source(self):
        body = ('<?xml version="1.0" encoding="UTF-8"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                '<w:body><w:p><w:r><w:t>가족 여행</w:t></w:r></w:p>'
                '<w:p><w:r><w:t>해변에서</w:t></w:r></w:p></w:body></w:document>')
        raw = io.BytesIO()
        with zipfile.ZipFile(raw, 'w') as archive:
            archive.writestr('word/document.xml', body)
        api = CaptureAPI()
        segments = list(iter_document_segments(raw.getvalue(), DOCX_MIME, '여행', MultimediaEmbeddings(api)))
        self.assertEqual(segments[0]['text'], '가족 여행\n해변에서')
        self.assertEqual(segments[0]['locator']['text_source'], 'docx/word/document.xml')

    def test_rejects_unsupported_empty_and_corrupt_content(self):
        embeddings = MultimediaEmbeddings(CaptureAPI())
        with self.assertRaises(ValueError):
            list(iter_document_segments(b'', 'text/plain', '문서', embeddings))
        with self.assertRaises(ValueError):
            list(iter_document_segments(b'example', 'application/zip', '문서', embeddings))
        with self.assertRaises(ValueError):
            list(iter_audio_segments(b'not audio', embeddings))

    def test_file_paths_and_more_than_one_batch_are_streamed(self):
        api = CaptureAPI()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source.txt'
            source.write_text('가' * 257, encoding='utf-8')
            result = list(iter_document_segments(source, 'text/plain', '문서', MultimediaEmbeddings(api), chunk_chars=1))
            self.assertEqual(len(result), 257)
            self.assertEqual(result[-1]['locator'], {'char_start': 256, 'char_end': 257})
            audio = Path(directory) / 'voice.wav'
            with wave.open(str(audio), 'wb') as f:
                f.setnchannels(1); f.setsampwidth(2); f.setframerate(16000)
                f.writeframes(struct.pack('<h', 0) * 32000)
            clips = list(iter_audio_segments(audio, MultimediaEmbeddings(api), chunk_seconds=1))
            self.assertEqual([x['locator'] for x in clips], [
                {'start_ms': 0, 'end_ms': 1000}, {'start_ms': 1000, 'end_ms': 2000}])
