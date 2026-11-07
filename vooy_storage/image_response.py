"""MCP image blocks and structured photo results for Vooy clients."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from mcp_types import CallToolResult, ImageContent, ResourceLink, TextContent


_IMAGE_MIMES = {'image/jpeg', 'image/png', 'image/webp', 'image/gif'}
_MAX_PREVIEW_BYTES = 2 * 1024 * 1024
_MAX_ORIGINAL_BYTES = 8 * 1024 * 1024


def _public_url(value):
    if not isinstance(value, str) or len(value) > 2048:
        return None
    try:
        parts = urlsplit(value)
        if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
            return None
        return urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))
    except ValueError:
        return None


def _oba_metadata(repository, asset_id, version_id):
    """Only expose selected provenance and labelled metadata for this exact version."""
    if not hasattr(repository, 'connect'):
        return {}
    with repository.connect() as conn:
        exists = conn.execute("SELECT to_regclass('oba_import_metadata') AS name").fetchone()
        if not exists or not exists['name']:
            return {}
        record = conn.execute('''SELECT o.photo_id,o.source,o.identities,o.metadata
            FROM oba_import_metadata o JOIN asset_versions v
              ON v.asset_id=o.asset_id AND v.sha256=o.sha256
            WHERE o.asset_id=%s AND v.id=%s''', (asset_id, version_id)).fetchone()
    if not record:
        return {}
    source = record.get('source') if isinstance(record.get('source'), dict) else {}
    public_source = {}
    for key in ('title', 'author', 'attribution', 'license_name'):
        value = source.get(key)
        if isinstance(value, str) and value:
            public_source[key] = value[:1000]
    for key in ('page_url', 'license_url', 'resolved_page_url'):
        value = _public_url(source.get(key))
        if value:
            public_source[key] = value
    identities = record.get('identities') or source.get('article_identities') or []
    names = []
    if isinstance(identities, list):
        for identity in identities[:100]:
            name = identity.get('name') if isinstance(identity, dict) else identity
            if isinstance(name, str) and name.strip():
                names.append(name.strip()[:200])
    metadata = record.get('metadata') if isinstance(record.get('metadata'), dict) else {}
    # Imported location/time may be simulated; keep their origin attached to every result.
    safe_metadata = {key: metadata[key] for key in ('origin', 'location', 'captured_at', 'event')
                     if isinstance(metadata.get(key), str)}
    return {'source_photo_id': record['photo_id'], 'source': public_source, 'identities': names,
            'metadata': safe_metadata}


def _image_block(data, mime):
    return ImageContent(type='image', data=base64.b64encode(data).decode('ascii'), mime_type=mime)


def _matches_image(data, mime):
    if mime == 'image/jpeg':
        return data.startswith(b'\xff\xd8\xff')
    if mime == 'image/png':
        return data.startswith(b'\x89PNG\r\n\x1a\n')
    if mime == 'image/gif':
        return data.startswith((b'GIF87a', b'GIF89a'))
    return mime == 'image/webp' and data[:4] == b'RIFF' and data[8:12] == b'WEBP'


def _read_bounded(path, maximum):
    """Bound reads even when a file changes between stat and read."""
    with Path(path).open('rb') as f:
        data = f.read(maximum + 1)
    return data if 0 < len(data) <= maximum else None


def _thumbnail(rag, asset_id, version_id):
    root = (rag.repository.media_root / 'derived').resolve()
    for item in rag.list_artifacts(asset_id):
        if item.get('kind') != 'thumbnail' or item.get('mime_type') not in _IMAGE_MIMES:
            continue
        artifact = rag.get_artifact(item['id'])
        if not artifact or str(artifact.get('asset_id')) != str(asset_id) or str(artifact.get('asset_version_id')) != str(version_id):
            continue
        path = (rag.repository.media_root / artifact['object_key']).resolve()
        if not path.is_relative_to(root):
            continue
        try:
            data = _read_bounded(path, _MAX_PREVIEW_BYTES)
        except OSError:
            continue
        if data and _matches_image(data, artifact['mime_type']) and (
                not artifact.get('sha256') or hashlib.sha256(data).hexdigest() == artifact['sha256']):
            return _image_block(data, artifact['mime_type'])
    return None


def _add_observations(result, captioner, entries):
    if captioner is None or not entries:
        return result
    try:
        descriptions = captioner.describe_many(entries)
        if len(descriptions) != len(entries):
            return result
        for item, observation in zip(entries, descriptions):
            if observation.get('photo_id') != item['photo_id'] or observation.get('version_id') != item['version_id']:
                continue
            for target in result.structured_content.get('results', [result.structured_content]):
                if str(target.get('asset_id')) == item['photo_id'] and str(target.get('version_id')) == item['version_id']:
                    target['visual_observation'] = observation
        result.content[0].text = json.dumps(result.structured_content, ensure_ascii=False, default=str)
    except Exception:
        # Image delivery remains available if the optional description service fails.
        pass
    return result


def search_result(payload, rag, max_images=3, captioner=None):
    """Return structured search results with up to three inline thumbnail blocks."""
    if not 0 <= max_images <= 5:
        raise ValueError('미리보기 수는 0~5여야 합니다.')
    value = copy.deepcopy(payload)
    blocks = []
    for row in value.get('results', []):
        row.pop('preview_content_index', None)
        if row.get('kind') != 'image':
            continue
        row.update(_oba_metadata(rag.repository, row['asset_id'], row['version_id']))
        if len(blocks) >= max_images:
            continue
        preview = _thumbnail(rag, row['asset_id'], row['version_id'])
        if preview:
            row['preview_content_index'] = len(blocks) + 1
            blocks.append(preview)
    text = TextContent(type='text', text=json.dumps(value, ensure_ascii=False, default=str))
    result = CallToolResult(content=[text, *blocks], structured_content=value)
    entries = []
    for row in value.get('results', []):
        index = row.get('preview_content_index')
        if isinstance(index, int) and 0 < index < len(result.content):
            block = result.content[index]
            entries.append({'photo_id': str(row['asset_id']), 'version_id': str(row['version_id']),
                            'image_bytes': base64.b64decode(block.data), 'mime_type': block.mime_type})
    return _add_observations(result, captioner, entries)


def photo_result(asset_id, repository, downloads, version_id=None, captioner=None):
    """Return a selected photo's bytes plus an expiring original link."""
    asset = repository.get_asset(asset_id, version_id) if version_id else repository.get_asset(asset_id)
    if asset is None or asset.get('kind') != 'image':
        raise ValueError('사진을 찾을 수 없습니다.')
    version_id = asset['version_id']
    link = downloads.link(asset_id, version_id)
    if not link:
        raise ValueError('사진의 원본을 찾을 수 없습니다.')
    image_link = downloads.image_link(asset_id, version_id) if hasattr(downloads, 'image_link') else link
    mime = (asset.get('mime_type') or '').lower().split(';', 1)[0].strip()
    value = {
        'photo_id': str(asset_id), 'asset_id': str(asset_id), 'version_id': str(version_id), 'title': asset.get('title'),
        'filename': asset.get('filename'), 'mime_type': mime, 'size_bytes': asset.get('size_bytes'),
        'sha256': asset.get('sha256'), 'image_url': image_link['url'] if image_link else None,
        'image_url_expires_at': image_link['expires_at'] if image_link else None,
        'download_url': link['url'], 'download_url_expires_at': link['expires_at'], 'delivery_status': 'not_sent',
        'image_content_included': False,
    }
    value.update(_oba_metadata(repository, asset_id, version_id))
    blocks = []
    if mime in _IMAGE_MIMES:
        try:
            data = _read_bounded(repository.asset_path(asset), _MAX_ORIGINAL_BYTES)
        except OSError:
            data = None
        if data and asset.get('sha256') and hashlib.sha256(data).hexdigest() != asset['sha256']:
            raise ValueError('사진 원본의 체크섬이 일치하지 않습니다.')
        if data and _matches_image(data, mime):
            value['image_content_included'] = True
            value['image_content_index'] = 1
            blocks.append(_image_block(data, mime))
    blocks.append(ResourceLink(type='resource_link', name=asset.get('filename') or 'image',
                               uri=image_link['url'] if image_link else link['url'], mime_type=mime or 'application/octet-stream'))
    text = TextContent(type='text', text=json.dumps(value, ensure_ascii=False, default=str))
    result = CallToolResult(content=[text, *blocks], structured_content=value)
    entries = []
    if value['image_content_included'] and len(data) <= 2 * 1024 * 1024 and mime in ('image/jpeg','image/png','image/webp'):
        entries.append({'photo_id': str(asset_id), 'version_id': str(version_id), 'image_bytes': data, 'mime_type': mime})
    return _add_observations(result, captioner, entries)
