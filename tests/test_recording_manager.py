import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "security-camera"))

from recording_manager import Recording, RecordingManager
from http_server import atomic_write_json
from http_server import INDEX_HTML_TEMPLATE


class SequenceEvent:
    def __init__(self, stop_after=1):
        self.calls = 0
        self.stop_after = stop_after

    def wait(self, timeout=None):
        self.calls += 1
        return self.calls > self.stop_after


class RecordingManagerTests(unittest.TestCase):
    def make_manager(self, directory):
        return RecordingManager(
            stream_url="http://camera.test/stream.m3u8",
            recordings_path=directory,
            pre_roll=10,
            post_roll=0,
            max_recordings=10,
        )

    def test_monitor_retries_when_process_is_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.make_manager(directory)
            manager._recording = True
            manager._ffmpeg_process = None
            manager._monitor_stop_event = SequenceEvent()
            replacement = Mock()
            with patch.object(manager, "_launch_ffmpeg_segment", return_value=replacement) as launch:
                manager._monitor_ffmpeg()
            launch.assert_called_once_with()
            self.assertIs(manager._ffmpeg_process, replacement)

    def test_monitor_keeps_retrying_after_a_failed_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.make_manager(directory)
            manager._recording = True
            manager._ffmpeg_process = None
            manager._monitor_stop_event = SequenceEvent(stop_after=2)
            replacement = Mock()
            with patch.object(
                manager, "_launch_ffmpeg_segment", side_effect=[None, replacement]
            ) as launch:
                manager._monitor_ffmpeg()
            self.assertEqual(launch.call_count, 2)
            self.assertIs(manager._ffmpeg_process, replacement)

    def test_long_motion_is_not_cut_at_five_minutes(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.make_manager(directory)
            manager._recording = True
            manager._recording_start_wall = 0
            manager._ffmpeg_process = Mock()
            manager._ffmpeg_process.poll.return_value = None
            manager._monitor_stop_event = SequenceEvent()
            with patch("recording_manager.time.time", return_value=10_000):
                manager._monitor_ffmpeg()
            self.assertTrue(manager._recording)

    def test_repeated_motion_keeps_the_same_recording(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.make_manager(directory)
            manager._recording = True
            manager._current_recording = Recording("existing.mp4", "existing.mp4", 1.0)
            timer = Mock()
            manager._stop_timer = timer
            with patch.object(manager, "_launch_ffmpeg_segment") as launch:
                manager.start_recording(motion_start_time=2.0)
            timer.cancel.assert_called_once_with()
            launch.assert_not_called()
            self.assertEqual(manager._current_recording.filename, "existing.mp4")

    def test_failed_concat_preserves_source_segments(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.make_manager(directory)
            first = Path(directory) / "one.ts"
            second = Path(directory) / "two.ts"
            first.write_bytes(b"one")
            second.write_bytes(b"two")
            with patch.object(manager, "_detect_segment_overlap", return_value=0), patch(
                "recording_manager.subprocess.run",
                return_value=SimpleNamespace(returncode=1, stderr="broken"),
            ):
                result = manager._concatenate_segments(
                    Path(directory) / "final.mp4", [str(first), str(second)]
                )
            self.assertFalse(result)
            self.assertTrue(first.exists())
            self.assertTrue(second.exists())

    def test_concat_excludes_detected_hls_overlap(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.make_manager(directory)
            first = Path(directory) / "one.ts"
            second = Path(directory) / "two.ts"
            first.write_bytes(b"one")
            second.write_bytes(b"two")
            captured = {}

            def run(cmd, **kwargs):
                concat_path = Path(cmd[cmd.index("-i") + 1])
                captured["manifest"] = concat_path.read_text()
                (Path(directory) / "final.mp4").write_bytes(b"valid")
                return SimpleNamespace(returncode=0, stderr="")

            with patch.object(manager, "_detect_segment_overlap", return_value=4.0), patch.object(
                manager, "_get_ffprobe_duration", return_value=10.0
            ), patch(
                "recording_manager.subprocess.run", side_effect=run
            ):
                result = manager._concatenate_segments(
                    Path(directory) / "final.mp4",
                    [str(first), str(second)],
                    damaged_segments={str(first)},
                )
            self.assertTrue(result)
            self.assertIn("inpoint 4.000", captured["manifest"])
            self.assertIn("outpoint 9.500", captured["manifest"])

    def test_monitor_restarts_a_process_that_stops_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.make_manager(directory)
            segment = Path(directory) / "stalled.ts"
            segment.write_bytes(b"unchanged")
            process = Mock()
            process.poll.return_value = None
            manager._recording = True
            manager._ffmpeg_process = process
            manager._active_segment_path = segment
            manager._last_segment_size = segment.stat().st_size
            manager._last_segment_growth = 0
            manager._stall_timeout = 20
            manager._monitor_stop_event = SequenceEvent()
            with patch("recording_manager.time.monotonic", return_value=30):
                manager._monitor_ffmpeg()
            process.terminate.assert_called_once_with()
            self.assertIsNone(manager._ffmpeg_process)

    def test_immediate_stop_waits_for_finalization(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.make_manager(directory)
            manager._recording = True
            manager._current_recording = Recording(
                "clip.mp4", str(Path(directory) / "clip.mp4"), 1.0
            )
            with patch.object(manager, "_finalize_recording") as finalize:
                manager.stop_recording_immediate()
            finalize.assert_called_once()

    def test_metadata_is_always_valid_json(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.make_manager(directory)
            video = Path(directory) / "clip.mp4"
            video.write_bytes(b"video")
            manager._recordings = [Recording("clip.mp4", str(video), 1.0)]
            manager._save_metadata()
            with manager.metadata_file.open() as handle:
                data = json.load(handle)
            self.assertEqual(data[0]["filename"], "clip.mp4")
            self.assertEqual(list(Path(directory).glob(".recordings.json.*.tmp")), [])

    def test_http_json_writes_are_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "recordings.json"
            atomic_write_json(path, [{"filename": "clip.mp4"}])
            self.assertEqual(json.loads(path.read_text())[0]["filename"], "clip.mp4")
            self.assertEqual(list(Path(directory).glob(".recordings.json.*.tmp")), [])

    def test_ui_contains_build_version_badge(self):
        html = INDEX_HTML_TEMPLATE.replace("%%APP_VERSION%%", "0.3.0")
        self.assertIn('class="version-badge">v0.3.0</span>', html)
        self.assertNotIn("%%APP_VERSION%%", html)


if __name__ == "__main__":
    unittest.main()
