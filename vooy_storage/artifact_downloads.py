"""Short-lived downloads for activated RAG artifacts."""
from urllib.parse import quote

from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route


class ArtifactDownloads:
    def __init__(self, rag, downloads):
        self.rag = rag
        self.downloads = downloads

    def link(self, artifact_id):
        artifact = self.rag.get_artifact(artifact_id)
        if artifact is None:
            return None
        token = self.downloads.issue('artifact:' + artifact_id)
        return {'url': f"{self.downloads.public_base_url}/api/v1/artifacts/{quote(artifact_id, safe='')}?token={token}",
                'expires_at': int(token.split('.', 1)[0])}

    def preview_link(self, artifact_id, version_id=None):
        artifact = self.rag.get_artifact(artifact_id)
        if artifact is None or artifact['kind'] != 'thumbnail' or artifact['mime_type'] not in ('image/jpeg', 'image/png', 'image/webp'):
            return None
        if version_id is not None and str(artifact['asset_version_id']) != str(version_id):
            return None
        token = self.downloads.issue('preview:' + artifact_id)
        return {'url': f"{self.downloads.public_base_url}/api/v1/artifacts/{quote(artifact_id, safe='')}/preview?token={token}",
                'expires_at': int(token.split('.', 1)[0]), 'mime_type': artifact['mime_type']}

    async def preview(self, request):
        artifact_id = request.path_params['artifact_id']
        if not self.downloads.valid('preview:' + artifact_id, request.query_params.get('token', '')):
            return JSONResponse({'error': 'invalid or expired link'}, status_code=403)
        artifact = self.rag.get_artifact(artifact_id)
        if artifact is None or artifact['kind'] != 'thumbnail' or artifact['mime_type'] not in ('image/jpeg', 'image/png', 'image/webp'):
            return JSONResponse({'error': 'preview not found'}, status_code=404)
        root = self.rag.repository.media_root / 'derived'
        path = (self.rag.repository.media_root / artifact['object_key']).resolve()
        if not path.is_relative_to(root.resolve()) or not path.is_file():
            return JSONResponse({'error': 'preview not found'}, status_code=404)
        return FileResponse(path, media_type=artifact['mime_type'], filename=artifact['filename'],
                            content_disposition_type='inline',
                            headers={'Cache-Control': 'private, no-store', 'Referrer-Policy': 'no-referrer',
                                     'X-Content-Type-Options': 'nosniff'})

    async def download(self, request):
        artifact_id = request.path_params['artifact_id']
        if not self.downloads.valid('artifact:' + artifact_id, request.query_params.get('token', '')):
            return JSONResponse({'error': 'invalid or expired link'}, status_code=403)
        artifact = self.rag.get_artifact(artifact_id)
        if artifact is None:
            return JSONResponse({'error': 'artifact not found'}, status_code=404)
        root = self.rag.repository.media_root / 'derived'
        path = (self.rag.repository.media_root / artifact['object_key']).resolve()
        if not path.is_relative_to(root.resolve()) or not path.is_file():
            return JSONResponse({'error': 'artifact not found'}, status_code=404)
        return FileResponse(path, media_type=artifact['mime_type'], filename=artifact['filename'],
                            headers={'Cache-Control': 'private, no-store', 'Referrer-Policy': 'no-referrer'})

    def routes(self):
        return [Route('/api/v1/artifacts/{artifact_id}', self.download, methods=['GET', 'HEAD']),
                Route('/api/v1/artifacts/{artifact_id}/preview', self.preview, methods=['GET', 'HEAD'])]
