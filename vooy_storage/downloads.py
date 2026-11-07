"""Short-lived, asset-specific download links."""

import hashlib
import hmac
import time
from urllib.parse import quote

from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route


class DownloadManager:
    def __init__(self, repository, public_base_url, secret, clock=time.time):
        self.repository = repository
        self.public_base_url = public_base_url.rstrip("/")
        self.secret = secret.encode("utf-8")
        self.clock = clock

    def issue(self, asset_id, version_id=None):
        expiry = int(self.clock()) + 900
        message = self._message(asset_id, expiry, version_id)
        signature = hmac.new(self.secret, message, hashlib.sha256).hexdigest()
        return f"{expiry}.{signature}"

    @staticmethod
    def _message(asset_id, expiry, version_id):
        return (f"{asset_id}:{version_id}:{expiry}" if version_id else f"{asset_id}:{expiry}").encode()

    def valid(self, asset_id, token, version_id=None):
        try:
            expiry_str, signature = token.split(".", 1)
            expiry = int(expiry_str)
        except (ValueError, AttributeError):
            return False
        if expiry <= int(self.clock()) or expiry > int(self.clock()) + 900:
            return False
        expected = hmac.new(self.secret, self._message(asset_id, expiry, version_id), hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature, expected)

    def link(self, asset_id, version_id=None):
        asset = self.repository.get_asset(asset_id, version_id) if version_id else self.repository.get_asset(asset_id)
        if asset is None:
            return None
        version_id = asset.get('version_id') or version_id
        token = self.issue(asset_id, version_id)
        suffix = f"&version_id={quote(version_id, safe='')}" if version_id else ''
        return {"url": f"{self.public_base_url}/api/v1/downloads/{quote(asset_id, safe='')}?token={token}{suffix}",
                "expires_at": int(token.split('.', 1)[0]), "version_id": version_id}

    def image_link(self, asset_id, version_id=None):
        """Issue a version-bound, inline image grant in a separate namespace."""
        asset = self.repository.get_asset(asset_id, version_id) if version_id else self.repository.get_asset(asset_id)
        if not asset or asset.get('kind') != 'image':
            return None
        mime = (asset.get('mime_type') or '').lower().split(';', 1)[0].strip()
        version_id = asset.get('version_id') or version_id
        if mime not in ('image/jpeg', 'image/png', 'image/webp', 'image/gif') or not version_id:
            return None
        token = self.issue('image:' + asset_id, version_id)
        return {'url': f"{self.public_base_url}/api/v1/images/{quote(asset_id, safe='')}?token={token}&version_id={quote(version_id, safe='')}",
                'expires_at': int(token.split('.', 1)[0]), 'version_id': version_id, 'mime_type': mime}

    async def image(self, request):
        asset_id = request.path_params['asset_id']
        version_id = request.query_params.get('version_id')
        if not version_id or not self.valid('image:' + asset_id, request.query_params.get('token', ''), version_id):
            return JSONResponse({'error': 'invalid or expired link'}, status_code=403)
        asset = self.repository.get_asset(asset_id, version_id)
        mime = (asset.get('mime_type') or '').lower().split(';', 1)[0].strip() if asset else ''
        if not asset or asset.get('kind') != 'image' or mime not in ('image/jpeg', 'image/png', 'image/webp', 'image/gif'):
            return JSONResponse({'error': 'image not found'}, status_code=404)
        try:
            path = self.repository.asset_path(asset)
            if not path.is_file():
                raise ValueError('image not found')
        except (OSError, ValueError):
            return JSONResponse({'error': 'image not found'}, status_code=404)
        return FileResponse(path, media_type=mime, filename=asset.get('filename') or 'image',
                            content_disposition_type='inline',
                            headers={'Cache-Control': 'private, no-store', 'Referrer-Policy': 'no-referrer',
                                     'X-Content-Type-Options': 'nosniff'})

    def media_link(self, asset_id, version_id=None):
        asset = self.repository.get_asset(asset_id, version_id) if version_id else self.repository.get_asset(asset_id)
        if not asset or asset.get('kind') not in ('video', 'audio', 'document'):
            return None
        mime = (asset.get('mime_type') or '').lower().split(';', 1)[0].strip()
        allowed = {'video/mp4', 'video/quicktime', 'video/webm', 'audio/mpeg', 'audio/wav',
                   'audio/x-wav', 'audio/mp4', 'application/pdf'}
        version_id = asset.get('version_id') or version_id
        if mime not in allowed or not version_id:
            return None
        token = self.issue('media:' + asset_id, version_id)
        return {'url': f"{self.public_base_url}/api/v1/media/{quote(asset_id, safe='')}?token={token}&version_id={quote(version_id, safe='')}",
                'expires_at': int(token.split('.', 1)[0]), 'version_id': version_id, 'mime_type': mime}

    async def media(self, request):
        asset_id = request.path_params['asset_id']
        version_id = request.query_params.get('version_id')
        if not version_id or not self.valid('media:' + asset_id, request.query_params.get('token', ''), version_id):
            return JSONResponse({'error': 'invalid or expired link'}, status_code=403)
        asset = self.repository.get_asset(asset_id, version_id)
        if not asset or not self.media_link(asset_id, version_id):
            return JSONResponse({'error': 'media not found'}, status_code=404)
        try:
            path = self.repository.asset_path(asset)
            if not path.is_file():
                raise ValueError('media not found')
        except (OSError, ValueError):
            return JSONResponse({'error': 'media not found'}, status_code=404)
        return FileResponse(path, media_type=asset['mime_type'], filename=asset.get('filename') or 'media',
                            content_disposition_type='inline',
                            headers={'Cache-Control': 'private, no-store', 'Referrer-Policy': 'no-referrer',
                                     'X-Content-Type-Options': 'nosniff'})

    async def download(self, request):
        asset_id = request.path_params["asset_id"]
        token = request.query_params.get("token", "")
        version_id = request.query_params.get("version_id")
        if not self.valid(asset_id, token, version_id):
            return JSONResponse({"error": "invalid or expired link"}, status_code=403)
        asset = self.repository.get_asset(asset_id, version_id) if version_id else self.repository.get_asset(asset_id)
        if asset is None:
            return JSONResponse({"error": "asset not found"}, status_code=404)
        if asset.get("kind") == "text" and asset.get("text") is not None:
            return Response(asset["text"].encode("utf-8"), media_type="text/plain; charset=utf-8",
                            headers={"Content-Disposition": 'attachment; filename="note.txt"',
                                     "Cache-Control": "private, no-store", "Referrer-Policy": "no-referrer"})
        path = self.repository.asset_path(asset)
        if not path.is_file():
            return JSONResponse({"error": "file not found"}, status_code=404)
        return FileResponse(path, media_type=asset.get("mime_type") or "application/octet-stream",
                            filename=asset.get("filename") or "download",
                            headers={"Cache-Control": "private, no-store", "Referrer-Policy": "no-referrer"})

    def routes(self):
        return [Route("/api/v1/downloads/{asset_id}", self.download, methods=["GET", "HEAD"]),
                Route("/api/v1/images/{asset_id}", self.image, methods=["GET", "HEAD"]),
                Route("/api/v1/media/{asset_id}", self.media, methods=["GET", "HEAD"])]
