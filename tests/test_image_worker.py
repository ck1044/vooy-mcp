import io
import json
import unittest

import httpx2
from PIL import Image
from PIL.TiffImagePlugin import IFDRational

from vooy_storage.image_worker import ImageWorker, prepare_image, extract_metadata


class Embeddings:
    def __init__(self):
        self.images = []
        self.texts = []
    def embed_image(self, value):
        self.images.append(value)
        return [0.6, 0.8]
    def embed_document(self, text, title=None):
        self.texts.append((text, title))
        return [0.6, 0.8]


class WorkerTest(unittest.TestCase):
    def test_capture_time_and_offset_are_extracted_separately(self):
        original = io.BytesIO()
        exif = Image.Exif()
        exif[0x9003] = '2024:10:31 13:20:10'
        exif[0x9011] = '+09:00'
        Image.new('RGB', (10, 10)).save(original, format='JPEG', exif=exif)
        self.assertEqual(extract_metadata(original.getvalue()), {
            'captured_at_local': '2024-10-31T13:20:10', 'utc_offset_minutes': 540,
            'latitude': None, 'longitude': None})

    def test_capture_coordinates_from_exif(self):
        original = io.BytesIO()
        exif = Image.Exif()
        exif[0x8825] = {1: 'S', 2: (IFDRational(37), IFDRational(30), IFDRational(0)),
                        3: 'E', 4: (IFDRational(127), IFDRational(0), IFDRational(0))}
        Image.new('RGB', (10, 10)).save(original, format='JPEG', exif=exif)
        facts = extract_metadata(original.getvalue())
        self.assertEqual(facts['latitude'], -37.5)
        self.assertEqual(facts['longitude'], 127.0)

    def test_image_worker_uploads_vector_and_thumbnail(self):
        output = io.BytesIO()
        Image.new('RGB', (1600, 900), 'red').save(output, format='PNG')
        raw = output.getvalue()
        requests = []
        def handle(request):
            requests.append(request)
            path = request.url.path
            payload = json.loads(request.content) if request.content and request.headers.get('content-type', '').startswith('application/json') else {}
            self.assertEqual(request.headers['authorization'], 'Bearer worker-token')
            if path.endswith('/claim'):
                return httpx2.Response(200, json={'id': 'job', 'lease_epoch': 1, 'space_id': 'space', 'asset_version_id': 'version'})
            if path.endswith('/source-grant'):
                return httpx2.Response(200, json={'kind': 'image', 'source_url': 'https://untrusted.example/image'})
            if path.endswith('/source'):
                self.assertEqual(request.url.host, 'worker.local')
                return httpx2.Response(200, content=raw)
            if path.endswith('/result-sets'):
                return httpx2.Response(200, json={'id': 'result'})
            if path.endswith('/artifacts'):
                self.assertEqual(payload['kind'], 'thumbnail')
                return httpx2.Response(200, json={'id': 'artifact'})
            if path.endswith('/batches/primary'):
                item = payload['segments'][0]
                self.assertEqual(item['kind'], 'image')
                self.assertEqual(item['locator']['bbox'], [0, 0, 1, 1])
                self.assertIsNone(item['text'])
                self.assertEqual(item['vectors'][0]['values'], [0.6, 0.8])
                return httpx2.Response(200, json={})
            if path.endswith('/result-sets/result/complete'):
                self.assertEqual(payload['manifest']['artifact_ids'], ['artifact'])
                self.assertEqual(payload['manifest']['vector_count'], 1)
                return httpx2.Response(200, json={})
            return httpx2.Response(200, json={})
        client = httpx2.Client(transport=httpx2.MockTransport(handle))
        embeddings = Embeddings()
        worker = ImageWorker('http://worker.local', 'worker-token', 'space', embeddings, client=client)
        self.assertTrue(worker.run_once())
        self.assertTrue(embeddings.images[0].startswith('data:image/jpeg;base64,'))
        self.assertTrue(any('/artifacts/artifact/parts/0' in str(r.url) for r in requests))
        self.assertFalse(any('/fail' in str(r.url) for r in requests))

    def test_provider_error_402_is_not_retried(self):
        requests = []
        class BadEmbeddings(Embeddings):
            def embed_document(self, text, title=None):
                req = httpx2.Request('POST', 'https://openrouter.ai/api/v1/embeddings')
                resp = httpx2.Response(402, request=req)
                raise httpx2.HTTPStatusError('payment required', request=req, response=resp)
        def handle(request):
            path = request.url.path
            payload = json.loads(request.content) if request.content else {}
            if path.endswith('/claim'):
                return httpx2.Response(200, json={'id': 'job', 'lease_epoch': 1, 'space_id': 'space', 'asset_version_id': 'version'})
            if path.endswith('/source-grant'):
                return httpx2.Response(200, json={'kind': 'text', 'text_body': '여행 추억', 'title': '여행'})
            if path.endswith('/result-sets'):
                return httpx2.Response(200, json={'id': 'result'})
            if path.endswith('/fail'):
                requests.append(payload)
            return httpx2.Response(200, json={})
        client = httpx2.Client(transport=httpx2.MockTransport(handle))
        worker = ImageWorker('http://worker.local', 'worker-token', 'space', BadEmbeddings(), client=client)
        worker.run_once()
        self.assertEqual(requests, [{'lease_epoch': 1, 'error_code': 'embedding_http_402', 'retryable': False}])

    def test_preview_removes_exif_and_scales(self):
        raw = io.BytesIO()
        img = Image.new('RGB', (2400, 800), 'blue')
        img.save(raw, format='JPEG', exif=b'Exif\x00\x00garbage')
        inference, thumbnail = prepare_image(raw.getvalue())
        with Image.open(io.BytesIO(inference)) as image:
            self.assertEqual(image.size, (2048, 683))
            self.assertNotIn('exif', image.info)
        with Image.open(io.BytesIO(thumbnail)) as image:
            self.assertLessEqual(max(image.size), 768)
            self.assertNotIn('exif', image.info)

    def test_heic_original_is_converted_for_embedding_and_preview(self):
        raw = io.BytesIO()
        Image.new('RGB', (80, 50), 'blue').save(raw, format='HEIF')
        inference, thumbnail = prepare_image(raw.getvalue())
        with Image.open(io.BytesIO(inference)) as image:
            self.assertEqual(image.format, 'JPEG')
            self.assertEqual(image.size, (80, 50))
        with Image.open(io.BytesIO(thumbnail)) as image:
            self.assertEqual(image.format, 'JPEG')


if __name__ == '__main__':
    unittest.main()
