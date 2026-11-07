import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from io import BytesIO

from vooy_storage.video_ingestion import (VideoProcessingError, frame_intervals,
                                          has_audio_stream, iter_video_frames, probe_video,
                                          video_frame_data_url, video_segment)


class VideoIntervalsTest(unittest.TestCase):
    def test_full_timeline_is_covered_without_empty_intervals(self):
        for duration in (1, 1000, 30_001, 7_200_000):
            chunks = list(frame_intervals(duration, interval_ms=10_000, max_frames=48))
            self.assertLessEqual(len(chunks), 48)
            self.assertEqual(chunks[0]['start_ms'], 0)
            self.assertEqual(chunks[-1]['end_ms'], duration)
            for i, chunk in enumerate(chunks):
                self.assertLessEqual(chunk['start_ms'], chunk['frame_time_ms'])
                self.assertLess(chunk['frame_time_ms'], chunk['end_ms'])
                if i:
                    self.assertEqual(chunks[i - 1]['end_ms'], chunk['start_ms'])

    def test_segment_preserves_original_timestamp_and_artifact(self):
        frame = {'index': 2, 'start_ms': 20000, 'end_ms': 30000,
                 'frame_time_ms': 24999, 'jpeg': b'jpeg'}
        segment = video_segment(frame, [0.6, 0.8], 'artifact-id')
        self.assertEqual(segment['kind'], 'video')
        self.assertEqual(segment['locator'], {'start_ms': 20000, 'end_ms': 30000,
                                               'frame_time_ms': 24999, 'artifact_id': 'artifact-id'})
        self.assertEqual(segment['vectors'][0]['values'], [0.6, 0.8])
        self.assertTrue(video_frame_data_url(frame).startswith('data:image/jpeg;base64,'))

    def test_invalid_duration_rejected(self):
        for bad in (0, -1, True, 1.5):
            with self.assertRaises(ValueError):
                list(frame_intervals(bad))

    def test_probe_uses_stream_duration_when_container_duration_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'clip.webm'
            path.touch()
            payload = b'{"format":{"duration":"N/A"},"streams":[{"codec_type":"video","duration":"2.5"}]}'
            with patch('vooy_storage.video_ingestion._run', return_value=payload):
                self.assertEqual(probe_video(path), 2500)


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'ffmpeg and ffprobe required')
class VideoFramesTest(unittest.TestCase):
    def test_real_video_extracts_timestamped_decodable_frames(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'clip.mp4'
            subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi',
                            '-i', 'color=c=blue:s=128x96:r=5:d=3', '-c:v', 'mpeg4',
                            '-y', str(path)], check=True)
            before = path.read_bytes()
            duration = probe_video(path)
            frames = list(iter_video_frames(path, interval_ms=1000, max_frames=3))
            self.assertEqual(len(frames), 3)
            self.assertEqual(frames[-1]['end_ms'], duration)
            self.assertEqual(path.read_bytes(), before)
            for frame in frames:
                with Image.open(BytesIO(frame['jpeg'])) as image:
                    self.assertEqual(image.format, 'JPEG')
                    self.assertLessEqual(image.width, 1024)

    def test_nonvideo_fails_cleanly(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'bad.mp4'
            path.write_bytes(b'not a video')
            with self.assertRaises(VideoProcessingError):
                list(iter_video_frames(path))

    def test_audio_stream_detection_for_video_with_and_without_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            silent = Path(directory) / 'silent.mp4'
            audible = Path(directory) / 'audible.mp4'
            subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi',
                            '-i', 'color=c=blue:s=128x96:r=5:d=1', '-c:v', 'mpeg4',
                            '-y', str(silent)], check=True)
            subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi',
                            '-i', 'color=c=blue:s=128x96:r=5:d=1', '-f', 'lavfi',
                            '-i', 'sine=frequency=440:duration=1', '-c:v', 'mpeg4',
                            '-c:a', 'aac', '-shortest', '-y', str(audible)], check=True)
            self.assertFalse(has_audio_stream(silent))
            self.assertTrue(has_audio_stream(audible))

    def test_corrupt_video_audio_detection_is_error_not_silence(self):
        with tempfile.TemporaryDirectory() as directory:
            broken = Path(directory) / 'bad.mp4'
            broken.write_bytes(b'not video')
            with self.assertRaises(VideoProcessingError):
                has_audio_stream(broken)


if __name__ == '__main__':
    unittest.main()
