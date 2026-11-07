"""MCP presentation for timestamped video search matches."""
from __future__ import annotations

import base64
import copy
import hashlib
import json

from mcp_types import CallToolResult, ImageContent, TextContent

MAX_FRAME_BYTES = 2 * 1024 * 1024


def _frame(rag, row):
    locator = row.get('locator') or {}
    artifact_id = locator.get('artifact_id')
    if not isinstance(artifact_id, str):
        return None
    artifact = rag.get_artifact(artifact_id)
    if not artifact or artifact.get('kind') != 'frame' or artifact.get('mime_type') != 'image/jpeg':
        return None
    if (str(artifact.get('asset_id')) != str(row.get('asset_id')) or
            str(artifact.get('asset_version_id')) != str(row.get('version_id'))):
        return None
    root = (rag.repository.media_root / 'derived').resolve()
    path = (rag.repository.media_root / artifact['object_key']).resolve()
    if not path.is_relative_to(root):
        return None
    try:
        with path.open('rb') as f:
            data = f.read(MAX_FRAME_BYTES + 1)
    except OSError:
        return None
    if (not data.startswith(b'\xff\xd8\xff') or len(data) > MAX_FRAME_BYTES or
            hashlib.sha256(data).hexdigest() != artifact.get('sha256')):
        return None
    return ImageContent(type='image', data=base64.b64encode(data).decode('ascii'),
                        mime_type='image/jpeg')


def video_search_result(payload, rag, max_frames=3):
    """Return sampled frames and original timeline positions without exposing paths."""
    if isinstance(max_frames, bool) or not isinstance(max_frames, int) or not 0 <= max_frames <= 5:
        raise ValueError('프레임 미리보기 수는 0~5여야 합니다.')
    result = copy.deepcopy(payload)
    blocks = []
    for row in result.get('results', []):
        row.pop('frame_content_index', None)
        if row.get('kind') != 'video' or len(blocks) >= max_frames:
            continue
        image = _frame(rag, row)
        if image:
            row['frame_content_index'] = len(blocks) + 1
            blocks.append(image)
    text = TextContent(type='text', text=json.dumps(result, ensure_ascii=False, default=str))
    return CallToolResult(content=[text, *blocks], structured_content=result)
