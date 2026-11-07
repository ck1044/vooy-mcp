import socket
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from vooy_storage.imports import ImportManager, validate_url


class ImportValidationTests(unittest.TestCase):
    def test_rejects_non_http_urls_and_credentials(self):
        for value in ('file:///etc/passwd', 'http://user:pass@example.com/file', 'http://localhost/file',
                      'http://127.0.0.1/test', 'http://169.254.169.254/latest', 'https://example.com:1234/file'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_url(value)

    @patch('vooy_storage.imports.socket.getaddrinfo')
    def test_rejects_dns_responses_containing_private_addresses(self, resolver):
        resolver.return_value = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443)),
                                 (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('10.0.0.2', 443))]
        with self.assertRaises(ValueError):
            validate_url('https://example.com/file')

    @patch('vooy_storage.imports.socket.getaddrinfo')
    def test_returns_validated_ip_for_pinned_connection(self, resolver):
        resolver.return_value = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443))]
        parsed, ip = validate_url('https://example.com/file?q=1')
        self.assertEqual((parsed.hostname, ip), ('example.com', '8.8.8.8'))

    def test_import_fetches_and_hashes_data(self):
        payload = b'\x89PNG\r\n\x1a\nimage-data'
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            def log_message(self, *args):
                pass
        class Repository:
            def save_file(self, title, filename, kind, path, size, sha, asset_id):
                self.result = (Path(path).read_bytes(), size, sha, kind)
                return {'id': asset_id}
            def update_asset(self, asset_id, source):
                self.source = source
        server = HTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with tempfile.TemporaryDirectory() as directory:
            repo = Repository()
            manager = ImportManager(repo, directory)
            url = f'http://127.0.0.1:{server.server_port}/photo.png?private=secret'
            with patch('vooy_storage.imports.validate_url', side_effect=lambda value: (urlsplit(value), '127.0.0.1')):
                job = manager.create('사진', url)
                for _ in range(100):
                    status = manager.status(job['job_id'])
                    if status['state'] in ('completed', 'failed'):
                        break
                    time.sleep(.02)
            self.assertEqual(status['state'], 'completed', status)
            self.assertEqual(repo.result, (payload, len(payload), hashlib.sha256(payload).hexdigest(), 'image/png'))
            self.assertNotIn('private', repo.source)


if __name__ == '__main__':
    unittest.main()
