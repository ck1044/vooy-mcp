"""Extract bounded, timestamped video frames for multimodal indexing.

The original file is never modified.  Frame images can be submitted to the
same image/text embedding space already used by the photo-search worker.
"""
from __future__ import annotations

import base64
import json
import math
import subprocess
from pathlib import Path


class VideoProcessingError(ValueError):
    """A video cannot be inspected or decoded."""


def _run(command, timeout):
    try:
        return subprocess.run(command, check=True, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=timeout).stdout
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        # stderr may contain local filenames or media metadata; do not expose it.
        raise VideoProcessingError('영상 정보를 읽거나 프레임을 추출할 수 없습니다.') from exc


def probe_video(source_path):
    """Return video duration in milliseconds; audio-only files are rejected."""
    path = Path(source_path)
    if not path.is_file():
        raise VideoProcessingError('영상 원본을 찾을 수 없습니다.')
    raw = _run(['ffprobe', '-v', 'error', '-protocol_whitelist', 'file,pipe',
                '-show_entries', 'format=duration:stream=codec_type,duration',
                '-of', 'json', str(path)], timeout=30)
    try:
        metadata = json.loads(raw)
        streams = metadata.get('streams', [])
        videos = [s for s in streams if s.get('codec_type') == 'video']
        if not videos:
            raise ValueError('no video stream')
        durations = [metadata.get('format', {}).get('duration')] + [s.get('duration') for s in videos]
        duration = None
        for value in durations:
            try:
                value = float(value)
            except (ValueError, TypeError):
                continue
            if math.isfinite(value) and value > 0:
                duration = value
                break
        if duration is None:
            raise ValueError('no duration')
        return max(1, math.ceil(duration * 1000))
    except (ValueError, TypeError, AttributeError, json.JSONDecodeError) as exc:
        raise VideoProcessingError('영상 길이를 확인할 수 없습니다.') from exc


def has_audio_stream(source_path):
    """Detect an audio stream in a local media file; invalid input raises an error."""
    path = Path(source_path)
    if not path.is_file():
        raise VideoProcessingError('영상 원본을 찾을 수 없습니다.')
    raw = _run(['ffprobe', '-v', 'error', '-protocol_whitelist', 'file,pipe',
                '-select_streams', 'a', '-show_entries', 'stream=index',
                '-of', 'json', str(path)], timeout=30)
    try:
        result = json.loads(raw)
        streams = result['streams']
        if not isinstance(streams, list) or any(not isinstance(item, dict) or
                                                not isinstance(item.get('index'), int) or
                                                isinstance(item.get('index'), bool) for item in streams):
            raise ValueError('invalid audio stream metadata')
        return bool(streams)
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise VideoProcessingError('영상의 오디오 트랙을 확인할 수 없습니다.') from exc


def frame_intervals(duration_ms, interval_ms=10_000, max_frames=48):
    """Cover the complete timeline with at most max_frames nonempty intervals."""
    if isinstance(duration_ms, bool) or not isinstance(duration_ms, int) or duration_ms < 1:
        raise ValueError('영상 길이는 양의 정수 밀리초여야 합니다.')
    if any(isinstance(v, bool) or not isinstance(v, int) or v < 1 for v in (interval_ms, max_frames)):
        raise ValueError('프레임 간격과 최대 개수는 양의 정수여야 합니다.')
    count = min(max_frames, max(1, math.ceil(duration_ms / interval_ms)), duration_ms)
    for index in range(count):
        start = duration_ms * index // count
        end = duration_ms * (index + 1) // count
        # Half-open interval, never seek exactly at the end of the video.
        yield {'index': index, 'start_ms': start, 'end_ms': end,
               'frame_time_ms': (start + end - 1) // 2}


def iter_video_frames(source_path, interval_ms=10_000, max_frames=48):
    """Yield representative JPEGs with their timeline ranges.

    For long videos the sampling interval expands to cover the whole timeline;
    the returned range is not evidence that a frame describes every moment.
    """
    path = Path(source_path)
    duration_ms = probe_video(path)
    for item in frame_intervals(duration_ms, interval_ms, max_frames):
        second = f"{item['frame_time_ms'] / 1000:.3f}"
        jpeg = _run(['ffmpeg', '-v', 'error', '-protocol_whitelist', 'file,pipe',
                     '-ss', second, '-i', str(path), '-map', '0:v:0', '-frames:v', '1',
                     '-vf', "scale='min(1024,iw)':-2", '-f', 'image2pipe',
                     '-vcodec', 'mjpeg', '-q:v', '4', 'pipe:1'], timeout=60)
        if not jpeg.startswith(b'\xff\xd8\xff'):
            raise VideoProcessingError('영상 프레임이 올바른 JPEG가 아닙니다.')
        yield {**item, 'jpeg': jpeg, 'duration_ms': duration_ms}


def video_segment(frame, embedding, artifact_id=None):
    """Build a result segment compatible with the existing worker API."""
    if not isinstance(embedding, list) or not embedding:
        raise ValueError('프레임 벡터가 필요합니다.')
    locator = {'start_ms': frame['start_ms'], 'end_ms': frame['end_ms'],
               'frame_time_ms': frame['frame_time_ms']}
    if artifact_id is not None:
        locator['artifact_id'] = str(artifact_id)
    return {'external_id': f"video-frame-{frame['index']:04d}", 'kind': 'video',
            'text': None, 'locator': locator,
            'vectors': [{'ordinal': 0, 'values': embedding}]}


def video_frame_data_url(frame):
    return 'data:image/jpeg;base64,' + base64.b64encode(frame['jpeg']).decode('ascii')
