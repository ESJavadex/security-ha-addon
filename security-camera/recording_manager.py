#!/usr/bin/env python3
"""
Recording manager for motion-triggered video clips.
Captures video from HLS stream when motion is detected.
"""

import os
import subprocess
import time
import threading
import logging
import json
import tempfile
from urllib.parse import urljoin
from urllib.request import urlopen
from datetime import datetime
from pathlib import Path
from typing import Optional, List, TYPE_CHECKING
from dataclasses import dataclass, asdict, field

if TYPE_CHECKING:
    from llm_analyzer import LLMAnalyzer, LLMAnalysisResult

logger = logging.getLogger(__name__)


@dataclass
class Recording:
    """Represents a recorded video clip."""
    filename: str
    filepath: str
    start_time: float
    end_time: Optional[float] = None
    duration: Optional[float] = None
    wall_clock_duration: Optional[float] = None  # Wall-clock time for reference
    filesize: Optional[int] = None
    thumbnail: Optional[str] = None
    screenshots: Optional[List[str]] = None  # List of screenshot filenames
    favorite: bool = False  # User-marked as important
    llm_analysis: Optional[dict] = None  # LLM analysis result

    def to_dict(self) -> dict:
        return asdict(self)


class RecordingManager:
    """
    Manages video recording when motion is detected.

    Features:
    - Pre-roll: captures X seconds before motion was triggered
    - Post-roll: continues recording X seconds after motion ends
    - Automatic cleanup of old recordings
    - Recording metadata stored in JSON
    """

    def __init__(
        self,
        stream_url: str,
        recordings_path: str = "/share/security_recordings",
        pre_roll: int = 10,
        post_roll: int = 0,
        max_recordings: int = 50,
        max_duration: int = 300,  # 5 minutes max per clip
        llm_analyzer: Optional['LLMAnalyzer'] = None,
        llm_auto_analyze: bool = False,
    ):
        """
        Initialize recording manager.

        Args:
            stream_url: HLS stream URL
            recordings_path: Directory to save recordings
            pre_roll: Seconds to capture before motion trigger
            post_roll: Seconds to continue after motion ends
            max_recordings: Maximum recordings to keep
            max_duration: Maximum recording duration in seconds
            llm_analyzer: Optional LLM analyzer for false positive detection
            llm_auto_analyze: Whether to automatically analyze new recordings
        """
        self.stream_url = stream_url
        self.recordings_path = Path(recordings_path)
        self.pre_roll = pre_roll
        self.post_roll = post_roll
        self.max_recordings = max_recordings
        self.max_duration = max_duration
        self.llm_analyzer = llm_analyzer
        self.llm_auto_analyze = llm_auto_analyze

        # State
        self._recording = False
        self._current_recording: Optional[Recording] = None
        self._ffmpeg_process: Optional[subprocess.Popen] = None
        self._stop_timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()
        self._transition_lock = threading.Lock()

        # Segment tracking for ffmpeg restart on stream interruptions
        self._segment_files: List[str] = []
        self._segment_index: int = 0
        self._monitor_thread: Optional[threading.Thread] = None
        self._monitor_stop_event = threading.Event()
        self._recording_start_wall: Optional[float] = None
        self._active_segment_path: Optional[Path] = None
        self._last_segment_size = 0
        self._last_segment_growth = time.monotonic()
        self._stall_timeout = 20.0
        self._damaged_segments = set()

        # Rolling buffer for pre-roll capture
        self._buffer_dir = self.recordings_path / "_buffer"
        self._buffer_dir.mkdir(parents=True, exist_ok=True)
        self._buffer_process: Optional[subprocess.Popen] = None
        self._buffer_thread: Optional[threading.Thread] = None
        self._buffer_running = False
        self._buffer_segment_secs = 5
        self._buffer_keep_count = max(1, (self.pre_roll // self._buffer_segment_secs) + 1)

        # Create recordings directory
        self.recordings_path.mkdir(parents=True, exist_ok=True)

        # Metadata file
        self.metadata_file = self.recordings_path / "recordings.json"
        self._recordings: List[Recording] = self._load_metadata()

        # Repair metadata durations in background (fixes wall-clock durations)
        if self._recordings:
            self.repair_metadata()

    def _load_metadata(self) -> List[Recording]:
        """Load recording metadata from JSON file, removing orphaned entries."""
        if self.metadata_file.exists():
            try:
                with open(self.metadata_file, 'r') as f:
                    data = json.load(f)

                recordings = []
                orphaned = 0
                for r in data:
                    rec = Recording(**r)
                    # Check if the video file still exists
                    video_path = Path(rec.filepath)
                    if video_path.exists():
                        recordings.append(rec)
                    else:
                        orphaned += 1
                        logger.debug(f"Removing orphaned metadata: {rec.filename}")

                # Save cleaned metadata if we removed orphans
                if orphaned > 0:
                    logger.info(f"Cleaned up {orphaned} orphaned recording entries")
                    self._recordings = recordings
                    self._save_metadata()

                return recordings
            except Exception as e:
                logger.error(f"Error loading metadata: {e}")
        return []

    def _save_metadata(self):
        """Save recording metadata atomically."""
        try:
            fd, temp_path = tempfile.mkstemp(
                dir=self.metadata_file.parent,
                prefix=f".{self.metadata_file.name}.",
                suffix=".tmp",
            )
            with os.fdopen(fd, 'w') as f:
                json.dump([r.to_dict() for r in self._recordings], f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, self.metadata_file)
        except Exception as e:
            logger.error(f"Error saving metadata: {e}")
            try:
                if 'temp_path' in locals() and os.path.exists(temp_path):
                    os.unlink(temp_path)
            except OSError:
                pass

    def _trigger_llm_analysis(self, filename: str, screenshots: List[str]):
        """
        Trigger async LLM analysis for a recording.

        Args:
            filename: Recording filename
            screenshots: List of screenshot filenames
        """
        if not self.llm_analyzer:
            return

        def analyze():
            try:
                self.llm_analyzer.mark_analysis_started(filename)
                result = self.llm_analyzer.analyze_recording(
                    filename, screenshots, self.recordings_path
                )
                self._update_recording_with_analysis(filename, result)
            except Exception as e:
                logger.error(f"LLM analysis failed for {filename}: {e}")
            finally:
                self.llm_analyzer.mark_analysis_complete(filename)

        thread = threading.Thread(target=analyze, daemon=True)
        thread.start()
        logger.info(f"LLM analysis started for {filename}")

    def _update_recording_with_analysis(self, filename: str, result: 'LLMAnalysisResult'):
        """
        Update recording metadata with LLM analysis result.

        Args:
            filename: Recording filename
            result: LLM analysis result
        """
        with self._lock:
            for recording in self._recordings:
                if recording.filename == filename:
                    recording.llm_analysis = result.to_dict()
                    self._save_metadata()
                    logger.info(
                        f"LLM analysis saved for {filename}: "
                        f"false_positive={result.is_false_positive}, "
                        f"confidence={result.confidence}"
                    )
                    break

    def analyze_recording_on_demand(self, filename: str) -> bool:
        """
        Trigger on-demand LLM analysis for a specific recording.

        Args:
            filename: Recording filename to analyze

        Returns:
            True if analysis was started, False if not possible
        """
        if not self.llm_analyzer:
            logger.warning("LLM analyzer not configured")
            return False

        # Find the recording
        recording = None
        for r in self._recordings:
            if r.filename == filename:
                recording = r
                break

        if not recording:
            logger.warning(f"Recording not found: {filename}")
            return False

        if not recording.screenshots:
            logger.warning(f"No screenshots available for {filename}")
            return False

        if self.llm_analyzer.is_analysis_pending(filename):
            logger.warning(f"Analysis already in progress for {filename}")
            return False

        self._trigger_llm_analysis(filename, recording.screenshots)
        return True

    def set_false_positive(self, filename: str, is_false_positive: bool) -> bool:
        """
        Manually set or clear false positive flag for a recording.

        Args:
            filename: Recording filename
            is_false_positive: Whether to mark as false positive

        Returns:
            True if updated, False if not found
        """
        with self._lock:
            for recording in self._recordings:
                if recording.filename == filename:
                    if recording.llm_analysis is None:
                        recording.llm_analysis = {}
                    recording.llm_analysis['is_false_positive'] = is_false_positive
                    recording.llm_analysis['confidence'] = 'manual'
                    recording.llm_analysis['description'] = 'Manually set by user'
                    self._save_metadata()
                    logger.info(f"Manual false positive set for {filename}: {is_false_positive}")
                    return True
        return False

    def _get_ffprobe_duration(self, video_path: Path) -> Optional[float]:
        """Get actual video duration using ffprobe.

        Returns:
            Duration in seconds, or None if ffprobe fails.
        """
        try:
            probe_cmd = [
                'ffprobe',
                '-v', 'error',
                '-show_entries', 'format=duration',
                '-of', 'default=noprint_wrappers=1:nokey=1',
                str(video_path)
            ]
            result = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=30)
            if result.returncode == 0 and result.stdout.strip():
                return float(result.stdout.strip())
        except Exception as e:
            logger.warning(f"ffprobe failed for {video_path}: {e}")
        return None

    def _launch_ffmpeg_segment(self) -> Optional[subprocess.Popen]:
        """Launch ffmpeg to record a single segment from the stream.

        Records without a time limit (-t flag removed); duration is managed
        explicitly by terminating the process when recording should stop.
        Adds HLS resilience flags for more reliable stream capture.

        Returns:
            The Popen process, or None if launch failed.
        """
        if not self._current_recording:
            return None

        segment_name = self._current_recording.filename.replace(
            '.mp4', f'_seg{self._segment_index:03d}.ts'
        )
        segment_path = self.recordings_path / segment_name
        self._segment_index += 1

        cmd = [
            'ffmpeg',
            '-rw_timeout', '10000000',
            '-i', self.stream_url,
            '-c', 'copy',
            '-f', 'mpegts',
            '-mpegts_flags', '+resend_headers',
            '-y',
            '-loglevel', 'error',
            str(segment_path),
        ]

        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self._segment_files.append(str(segment_path))
            self._active_segment_path = segment_path
            self._last_segment_size = 0
            self._last_segment_growth = time.monotonic()
            logger.info(f"ffmpeg segment started: {segment_name} (PID {proc.pid})")
            return proc
        except Exception as e:
            logger.error(f"Failed to launch ffmpeg segment: {e}")
            return None

    def _capture_hls_preroll(self) -> List[str]:
        """Download closed HLS segments covering the configured pre-roll."""
        if self.pre_roll <= 0 or not self._current_recording:
            return []

        try:
            playlist_url = self.stream_url
            with urlopen(playlist_url, timeout=5) as response:
                playlist_text = response.read().decode('utf-8', errors='replace')
            lines = [line.strip() for line in playlist_text.splitlines() if line.strip()]

            # Follow one master-playlist level when necessary.
            if not any(line.startswith('#EXTINF:') for line in lines):
                child = next((line for line in lines if not line.startswith('#')), None)
                if not child:
                    return []
                playlist_url = urljoin(playlist_url, child)
                with urlopen(playlist_url, timeout=5) as response:
                    playlist_text = response.read().decode('utf-8', errors='replace')
                lines = [line.strip() for line in playlist_text.splitlines() if line.strip()]

            segments = []
            pending_duration = None
            for line in lines:
                if line.startswith('#EXTINF:'):
                    try:
                        pending_duration = float(line.split(':', 1)[1].split(',', 1)[0])
                    except ValueError:
                        pending_duration = None
                elif not line.startswith('#') and pending_duration is not None:
                    segments.append((pending_duration, urljoin(playlist_url, line)))
                    pending_duration = None

            selected = []
            accumulated = 0.0
            for duration, uri in reversed(segments):
                selected.append((duration, uri))
                accumulated += duration
                if accumulated >= self.pre_roll:
                    break
            selected.reverse()
            if not selected:
                return []

            segment_name = self._current_recording.filename.replace(
                '.mp4', f'_seg{self._segment_index:03d}.ts'
            )
            segment_path = self.recordings_path / segment_name
            temp_path = segment_path.with_suffix('.ts.part')
            with open(temp_path, 'wb') as output:
                for _, uri in selected:
                    with urlopen(uri, timeout=10) as segment_response:
                        output.write(segment_response.read())
                output.flush()
                os.fsync(output.fileno())
            os.replace(temp_path, segment_path)
            self._segment_index += 1
            self._segment_files.append(str(segment_path))
            logger.info(
                f"Captured {accumulated:.1f}s HLS pre-roll in {len(selected)} segments"
            )
            return [str(segment_path)]
        except Exception as e:
            logger.warning(f"Could not capture HLS pre-roll: {e}")
            try:
                if 'temp_path' in locals() and temp_path.exists():
                    temp_path.unlink()
            except OSError:
                pass
            return []

    def _monitor_ffmpeg(self):
        """Monitor ffmpeg process and restart if it dies during recording.

        Runs in a background thread. Checks every 2 seconds whether ffmpeg
        is still alive. If it has exited unexpectedly, starts a new segment.
        It also restarts a process that remains alive but stops writing data.
        """
        logger.debug("ffmpeg monitor thread started")
        while not self._monitor_stop_event.wait(timeout=2.0):
            with self._lock:
                if not self._recording:
                    break
                if self._ffmpeg_process is None:
                    logger.info("No active ffmpeg process, retrying recording segment...")
                    self._ffmpeg_process = self._launch_ffmpeg_segment()
                    continue

                retcode = self._ffmpeg_process.poll()
                if retcode is not None:
                    # ffmpeg has exited unexpectedly
                    stderr_output = ""
                    try:
                        stderr_output = self._ffmpeg_process.stderr.read().decode(errors='replace')[:500]
                    except Exception:
                        pass
                    logger.warning(
                        f"ffmpeg exited with code {retcode} during active recording. "
                        f"stderr: {stderr_output}"
                    )
                    if self._active_segment_path and retcode != 0:
                        self._damaged_segments.add(str(self._active_segment_path))
                    self._active_segment_path = None

                    # Attempt restart with new segment
                    logger.info("Restarting ffmpeg for new segment...")
                    new_proc = self._launch_ffmpeg_segment()
                    if new_proc:
                        self._ffmpeg_process = new_proc
                        logger.info("ffmpeg restarted successfully")
                    else:
                        logger.error("ffmpeg restart failed, will retry in 2s")
                        self._ffmpeg_process = None
                elif self._active_segment_path:
                    try:
                        current_size = self._active_segment_path.stat().st_size
                    except OSError:
                        current_size = 0
                    now = time.monotonic()
                    if current_size > self._last_segment_size:
                        self._last_segment_size = current_size
                        self._last_segment_growth = now
                    elif now - self._last_segment_growth >= self._stall_timeout:
                        logger.error(
                            f"ffmpeg stalled for {self._stall_timeout:.0f}s; restarting segment"
                        )
                        self._damaged_segments.add(str(self._active_segment_path))
                        self._ffmpeg_process.terminate()
                        self._ffmpeg_process = None
                        self._active_segment_path = None
        logger.debug("ffmpeg monitor thread stopped")

    def _concatenate_segments(
        self,
        final_path: Path,
        segment_files: List[str],
        damaged_segments=None,
    ) -> bool:
        """Concatenate multiple segment files into a single MP4.

        Uses ffmpeg concat demuxer. Filters out empty or missing segments.

        Args:
            final_path: Desired output file path.

        Returns:
            True if concatenation succeeded, False otherwise.
        """
        # Filter out empty or missing segment files
        valid_segments = []
        for seg_path_str in segment_files:
            seg_path = Path(seg_path_str)
            if seg_path.exists() and seg_path.stat().st_size > 0:
                valid_segments.append(seg_path_str)
            else:
                logger.warning(f"Skipping empty/missing segment: {seg_path_str}")

        if not valid_segments:
            logger.error("No valid segments to concatenate")
            return False

        if len(valid_segments) == 1:
            single = Path(valid_segments[0])
            success = self._remux_segment(single, final_path)
            if success:
                self._cleanup_segment_files(segment_files, keep=final_path)
            return success

        # Write concat list file for ffmpeg
        concat_list_path = self.recordings_path / f"_concat_{final_path.stem}.txt"
        concat_succeeded = False
        damaged_segments = set(damaged_segments or [])
        try:
            with open(concat_list_path, 'w') as f:
                previous = None
                for seg in valid_segments:
                    escaped = seg.replace("'", "'\\''")
                    f.write(f"file '{escaped}'\n")
                    if seg in damaged_segments:
                        segment_duration = self._get_ffprobe_duration(Path(seg))
                        if segment_duration and segment_duration > 0.75:
                            f.write(f"outpoint {segment_duration - 0.5:.3f}\n")
                            logger.warning(
                                f"Trimming damaged tail from {Path(seg).name}"
                            )
                    if previous:
                        overlap = self._detect_segment_overlap(Path(previous), Path(seg))
                        if overlap > 0:
                            # Concat demuxer applies inpoint to the file line
                            # immediately preceding this directive.
                            f.write(f"inpoint {overlap:.3f}\n")
                            logger.warning(
                                f"Removing {overlap:.1f}s HLS overlap before {Path(seg).name}"
                            )
                    previous = seg

            cmd = [
                'ffmpeg',
                '-f', 'concat',
                '-safe', '0',
                '-i', str(concat_list_path),
                '-c', 'copy',
                '-movflags', '+faststart',
                '-y',
                '-loglevel', 'error',
                str(final_path)
            ]

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode != 0:
                logger.error(f"Concat failed: {result.stderr}")
                return False

            output_duration = self._get_ffprobe_duration(final_path)
            if (
                not final_path.exists()
                or final_path.stat().st_size == 0
                or not output_duration
            ):
                logger.error("Concat produced an invalid or empty MP4; preserving source segments")
                return False

            concat_succeeded = True
            logger.info(f"Concatenated {len(valid_segments)} segments into {final_path.name}")
            return True

        except Exception as e:
            logger.error(f"Concatenation error: {e}")
            return False

        finally:
            # Clean up concat list file
            try:
                concat_list_path.unlink(missing_ok=True)
            except Exception:
                pass

            # Preserve source segments on failure so recovery remains possible.
            if concat_succeeded:
                for seg in segment_files:
                    try:
                        seg_path = Path(seg)
                        if seg_path.exists() and seg_path != final_path:
                            seg_path.unlink()
                    except Exception as e:
                        logger.warning(f"Failed to clean up segment {seg}: {e}")

    def _cleanup_segment_files(self, segment_files: List[str], keep: Optional[Path] = None):
        """Remove temporary segments after a verified final file exists."""
        for seg in segment_files:
            try:
                seg_path = Path(seg)
                if seg_path.exists() and seg_path != keep:
                    seg_path.unlink()
            except Exception as e:
                logger.warning(f"Failed to clean up segment {seg}: {e}")

    def _remux_segment(self, segment_path: Path, final_path: Path) -> bool:
        """Convert a crash-tolerant TS segment into the final MP4 container."""
        cmd = [
            'ffmpeg',
            '-i', str(segment_path),
            '-c', 'copy',
            '-movflags', '+faststart',
            '-avoid_negative_ts', 'make_zero',
            '-y',
            '-loglevel', 'error',
            str(final_path),
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode == 0 and final_path.exists() and final_path.stat().st_size > 0:
                return True
            logger.error(f"Segment remux failed: {result.stderr}")
        except Exception as e:
            logger.error(f"Segment remux error: {e}")
        return False

    def _frame_hashes(
        self,
        video_path: Path,
        start: float,
        duration: float,
        sample_fps: int = 2,
    ) -> List[str]:
        """Return deterministic hashes for a short, low-resolution video window."""
        cmd = [
            'ffmpeg',
            '-ss', f'{max(0.0, start):.3f}',
            '-i', str(video_path),
            '-t', f'{max(0.1, duration):.3f}',
            '-vf', f'fps={sample_fps},scale=160:-2',
            '-f', 'framemd5',
            '-loglevel', 'error',
            '-',
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode != 0:
                return []
            return [
                line.rsplit(',', 1)[-1].strip()
                for line in result.stdout.splitlines()
                if line and not line.startswith('#')
            ]
        except Exception as e:
            logger.debug(f"Could not fingerprint {video_path.name}: {e}")
            return []

    def _detect_segment_overlap(
        self,
        previous: Path,
        current: Path,
        max_seconds: int = 20,
        sample_fps: int = 2,
    ) -> float:
        """Detect exact HLS content replayed across a reconnect boundary."""
        previous_duration = self._get_ffprobe_duration(previous)
        current_duration = self._get_ffprobe_duration(current)
        if not previous_duration or not current_duration:
            return 0.0

        window = min(float(max_seconds), previous_duration, current_duration)
        if window < 2:
            return 0.0
        previous_hashes = self._frame_hashes(
            previous,
            max(0.0, previous_duration - window),
            window,
            sample_fps,
        )
        current_hashes = self._frame_hashes(current, 0.0, window, sample_fps)
        maximum = min(len(previous_hashes), len(current_hashes))
        minimum = sample_fps * 2  # Require at least two exact seconds.
        for count in range(maximum, minimum - 1, -1):
            if previous_hashes[-count:] == current_hashes[:count]:
                return count / sample_fps
        return 0.0

    def repair_metadata(self):
        """Re-calculate duration for existing recordings using ffprobe.

        Fixes recordings that have inaccurate wall-clock durations.
        Runs in a background thread to avoid blocking startup.
        """
        def _do_repair():
            repaired = 0
            errors = 0
            for recording in self._recordings:
                filepath = Path(recording.filepath)
                if not filepath.exists():
                    continue

                actual_duration = self._get_ffprobe_duration(filepath)
                if actual_duration is None:
                    errors += 1
                    continue

                with self._lock:
                    # Store old duration as wall_clock if not already set
                    if recording.wall_clock_duration is None and recording.duration is not None:
                        recording.wall_clock_duration = recording.duration

                    # Only update if the difference is significant (>10% and >5s)
                    if recording.duration is not None:
                        diff = abs(recording.duration - actual_duration)
                        pct = diff / max(recording.duration, 1) * 100
                        if diff > 5 and pct > 10:
                            logger.info(
                                f"Repairing {recording.filename}: "
                                f"old={recording.duration:.1f}s, actual={actual_duration:.1f}s "
                                f"(diff={diff:.1f}s, {pct:.0f}%)"
                            )
                            recording.duration = actual_duration
                            repaired += 1
                    else:
                        recording.duration = actual_duration
                        repaired += 1

            if repaired > 0:
                with self._lock:
                    self._save_metadata()
                logger.info(f"Metadata repair complete: {repaired} recordings updated, {errors} errors")
            else:
                logger.info(f"Metadata repair: no corrections needed ({errors} errors)")

        thread = threading.Thread(target=_do_repair, daemon=True)
        thread.start()
        logger.info("Starting metadata repair in background thread")

    def _generate_filename(self) -> str:
        """Generate a unique filename for the recording."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return f"motion_{timestamp}.mp4"

    def _generate_thumbnail(self, video_path: Path) -> Optional[str]:
        """Generate a thumbnail from the video (legacy, returns first screenshot)."""
        screenshots = self._generate_screenshots(video_path)
        if screenshots:
            return screenshots[0]
        return None

    def _generate_screenshots(self, video_path: Path, interval: int = 5) -> List[str]:
        """
        Generate multiple screenshots from the video at regular intervals.

        Args:
            video_path: Path to the video file
            interval: Seconds between screenshots (default 5)

        Returns:
            List of screenshot file paths
        """
        screenshots = []
        base_name = video_path.stem  # e.g., "motion_20241127_143022"

        screenshot_pattern = video_path.parent / f"{base_name}_%03d.jpg"
        try:
            # Decode the file once. The previous implementation reopened and
            # decoded from the beginning for every screenshot, blocking motion
            # callbacks for minutes on longer recordings.
            cmd = [
                'ffmpeg',
                '-i', str(video_path),
                '-vf', f'fps=1/{max(1, interval)},scale=320:-1',
                '-q:v', '4',
                '-start_number', '0',
                '-y',
                '-loglevel', 'error',
                str(screenshot_pattern),
            ]
            subprocess.run(cmd, timeout=120, check=True)
            screenshots = [p.name for p in sorted(video_path.parent.glob(f"{base_name}_*.jpg"))]
        except Exception as e:
            logger.error(f"Error generating screenshots for {video_path.name}: {e}")

        logger.info(f"Generated {len(screenshots)} screenshots for {video_path.name}")
        return screenshots

    def _cleanup_old_recordings(self):
        """Remove old recordings based on limits and false positive age."""
        recordings_to_remove = []

        # 1. Auto-delete false positives older than 72 hours
        now = time.time()
        fp_max_age_hours = 72
        fp_max_age_seconds = fp_max_age_hours * 3600

        for recording in self._recordings:
            if recording.llm_analysis:
                analysis = recording.llm_analysis
                is_fp = analysis.get('is_false_positive', False)
                if is_fp and recording.start_time:
                    age_seconds = now - recording.start_time
                    if age_seconds > fp_max_age_seconds:
                        recordings_to_remove.append(recording)
                        logger.info(f"Auto-removing false positive (>72h): {recording.filename}")

        # 2. Remove oldest if exceeding max_recordings (0 = unlimited)
        if self.max_recordings > 0:
            remaining = [r for r in self._recordings if r not in recordings_to_remove]
            if len(remaining) > self.max_recordings:
                sorted_recordings = sorted(remaining, key=lambda r: r.start_time)
                to_remove_count = len(remaining) - self.max_recordings
                for recording in sorted_recordings[:to_remove_count]:
                    if recording not in recordings_to_remove:
                        recordings_to_remove.append(recording)
                        logger.info(f"Removing old recording (over limit): {recording.filename}")

        # Delete the recordings
        for recording in recordings_to_remove:
            self._delete_recording_files(recording)
            if recording in self._recordings:
                self._recordings.remove(recording)

        if recordings_to_remove:
            self._save_metadata()

    def _delete_recording_files(self, recording):
        """Delete all files associated with a recording."""
        try:
            # Delete video file
            video_path = Path(recording.filepath)
            if video_path.exists():
                video_path.unlink()

            # Delete all screenshots
            if recording.screenshots:
                for screenshot in recording.screenshots:
                    screenshot_path = video_path.parent / screenshot
                    if screenshot_path.exists():
                        screenshot_path.unlink()
            elif recording.thumbnail:
                # Fallback for old recordings with single thumbnail
                thumb_path = Path(recording.thumbnail)
                if thumb_path.exists():
                    thumb_path.unlink()

        except Exception as e:
            logger.error(f"Error removing recording files {recording.filename}: {e}")

    # ── Rolling pre-roll buffer ────────────────────────────────────────

    def start_buffer(self):
        """Start the rolling pre-roll buffer in a background thread.

        The buffer continuously records short segments (5s each) from the
        stream using ffmpeg -c copy (no transcoding). Old segments are
        pruned to keep only the most recent ones (enough for pre_roll seconds).
        The buffer pauses automatically while a recording is active.
        """
        if self._buffer_running:
            logger.warning("Buffer already running")
            return
        if self.pre_roll <= 0:
            logger.info("Pre-roll is 0, buffer disabled")
            return

        self._buffer_running = True
        self._buffer_thread = threading.Thread(target=self._run_buffer, daemon=True)
        self._buffer_thread.start()
        logger.info(
            f"Rolling buffer started ({self.pre_roll}s pre-roll, "
            f"{self._buffer_segment_secs}s segments, keep {self._buffer_keep_count})"
        )

    def stop_buffer(self):
        """Stop the rolling pre-roll buffer."""
        self._buffer_running = False
        self._stop_buffer_ffmpeg()
        if self._buffer_thread:
            self._buffer_thread.join(timeout=10)
            self._buffer_thread = None
        # Clean up any leftover buffer files
        self._clean_buffer_dir()
        logger.info("Rolling buffer stopped")

    def _run_buffer(self):
        """Background thread: keeps buffer ffmpeg alive, prunes old segments.

        Pauses when self._recording is True (buffer not needed during active
        recording) and restarts automatically when recording ends.
        """
        logger.debug("Buffer thread started")
        while self._buffer_running:
            try:
                if self._recording:
                    # Recording active — pause buffer, wait and check again
                    self._stop_buffer_ffmpeg()
                    time.sleep(2)
                    continue

                # Ensure buffer ffmpeg is running
                if self._buffer_process is None or self._buffer_process.poll() is not None:
                    if self._buffer_process is not None:
                        # Previous process died, log it
                        retcode = self._buffer_process.poll()
                        logger.debug(f"Buffer ffmpeg exited (code {retcode}), restarting")
                    self._launch_buffer_ffmpeg()

                # Prune old segments
                self._prune_buffer_segments()

            except Exception as e:
                logger.error(f"Buffer thread error: {e}")

            time.sleep(2)

        logger.debug("Buffer thread exiting")

    def _launch_buffer_ffmpeg(self):
        """Start ffmpeg to write rolling segments into _buffer/ dir.

        Uses ffmpeg segment muxer with -c copy for negligible CPU usage.
        Segment filenames include a timestamp prefix for chronological ordering.
        """
        self._stop_buffer_ffmpeg()  # Kill any stale process

        ts_prefix = datetime.now().strftime("%Y%m%d_%H%M%S")
        segment_pattern = str(self._buffer_dir / f"buf_{ts_prefix}_%04d.mp4")

        cmd = [
            'ffmpeg',
            '-rw_timeout', '10000000',
            '-i', self.stream_url,
            '-c', 'copy',
            '-f', 'segment',
            '-segment_time', str(self._buffer_segment_secs),
            '-reset_timestamps', '1',
            '-movflags', '+faststart',
            '-loglevel', 'error',
            '-y',
            segment_pattern
        ]

        try:
            self._buffer_process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            logger.debug(f"Buffer ffmpeg launched (PID {self._buffer_process.pid})")
        except Exception as e:
            logger.error(f"Failed to launch buffer ffmpeg: {e}")
            self._buffer_process = None

    def _stop_buffer_ffmpeg(self):
        """Terminate the buffer ffmpeg process gracefully."""
        if self._buffer_process is None:
            return

        try:
            self._buffer_process.terminate()
            try:
                self._buffer_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._buffer_process.kill()
                try:
                    self._buffer_process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
        except Exception as e:
            logger.warning(f"Error stopping buffer ffmpeg: {e}")
        finally:
            self._buffer_process = None

    def _prune_buffer_segments(self):
        """Keep only the latest N+1 buffer segments, delete older ones.

        Segments are sorted by filename (which embeds a timestamp).
        We keep one extra segment beyond what's needed because the newest
        segment is still being written to and may be incomplete.
        """
        try:
            segments = sorted(self._buffer_dir.glob("buf_*.mp4"))
            keep = self._buffer_keep_count + 1  # +1 for the segment currently being written
            if len(segments) > keep:
                for old_seg in segments[:-keep]:
                    try:
                        old_seg.unlink()
                        logger.debug(f"Pruned old buffer segment: {old_seg.name}")
                    except Exception as e:
                        logger.warning(f"Failed to prune {old_seg.name}: {e}")
        except Exception as e:
            logger.warning(f"Error pruning buffer segments: {e}")

    def _clean_buffer_dir(self):
        """Remove all files from the buffer directory."""
        try:
            for f in self._buffer_dir.glob("*"):
                try:
                    f.unlink()
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"Error cleaning buffer dir: {e}")

    def _capture_pre_roll(self) -> List[str]:
        """Stop the buffer ffmpeg and collect valid segments for pre-roll.

        Kills the buffer ffmpeg process (which finalizes the current segment),
        then moves all valid (non-empty) buffer segments into the recordings
        directory with a _preroll_ prefix.

        Returns:
            List of pre-roll segment file paths (chronological order).
        """
        pre_roll_files: List[str] = []

        # Stop buffer ffmpeg so the current segment is finalized
        self._stop_buffer_ffmpeg()

        # Brief pause to let the filesystem flush
        time.sleep(0.3)

        try:
            segments = sorted(self._buffer_dir.glob("buf_*.mp4"))
            if not segments:
                logger.info("No buffer segments available for pre-roll")
                return pre_roll_files

            # Take only the last N segments (enough for pre_roll seconds)
            segments = segments[-self._buffer_keep_count:]

            for idx, seg_path in enumerate(segments):
                if not seg_path.exists():
                    continue
                if seg_path.stat().st_size == 0:
                    logger.debug(f"Skipping empty buffer segment: {seg_path.name}")
                    continue

                dest_name = f"_preroll_{idx:03d}.mp4"
                dest_path = self.recordings_path / dest_name
                try:
                    seg_path.rename(dest_path)
                    pre_roll_files.append(str(dest_path))
                    logger.debug(f"Pre-roll segment: {seg_path.name} → {dest_name}")
                except OSError as e:
                    logger.warning(f"Failed to move buffer segment {seg_path.name}: {e}")

            logger.info(f"Captured {len(pre_roll_files)} pre-roll segments")

        except Exception as e:
            logger.error(f"Error capturing pre-roll: {e}")

        # Clean up any remaining buffer files
        self._clean_buffer_dir()

        return pre_roll_files

    # ── Recording management ────────────────────────────────────────────

    def start_recording(self, motion_start_time: Optional[float] = None):
        """
        Start recording video from stream.

        Uses a segment-based approach: ffmpeg records to segment files, and a
        monitor thread watches for ffmpeg crashes and restarts it automatically.
        On stop, segments are concatenated into a single MP4.

        If a recording is already active, it is finalized first and a new one
        is started (split recording on new motion event).

        Args:
            motion_start_time: Timestamp when motion was first detected (for pre-roll)
        """
        with self._transition_lock, self._lock:
            if self._recording:
                # Repeated motion belongs to the same event. Never split a clip
                # or reconnect to HLS while motion is still active.
                if self._stop_timer:
                    self._stop_timer.cancel()
                    self._stop_timer = None
                logger.debug("Recording already active; continuing current event")
                return

            self._recording = True
            self._segment_index = 0
            self._recording_start_wall = time.time()

            filename = self._generate_filename()
            filepath = self.recordings_path / filename

            self._current_recording = Recording(
                filename=filename,
                filepath=str(filepath),
                start_time=motion_start_time or time.time()
            )

            logger.info(f"Starting recording: {filename}")

            # Pre-roll is handled by the HLS playlist window: ffmpeg naturally
            # closed segments are downloaded explicitly. The live ffmpeg input
            # may replay part of that window; concat removes exact overlap.
            self._segment_files = []
            self._capture_hls_preroll()

            # Launch first live recording segment
            self._ffmpeg_process = self._launch_ffmpeg_segment()
            if self._ffmpeg_process is None:
                # Keep the event active: the monitor retries until the stream
                # recovers instead of silently losing the whole event.
                logger.error("Initial ffmpeg start failed; monitor will retry")

            # Start monitor thread to watch for ffmpeg crashes
            self._monitor_stop_event.clear()
            self._monitor_thread = threading.Thread(
                target=self._monitor_ffmpeg, daemon=True
            )
            self._monitor_thread.start()

    def extend_recording(self):
        """Extend recording by canceling any pending stop."""
        with self._lock:
            if self._stop_timer:
                self._stop_timer.cancel()
                self._stop_timer = None
                logger.debug("Recording extended, stop timer canceled")

    def schedule_stop(self):
        """Schedule recording to stop after post_roll seconds."""
        with self._lock:
            if not self._recording:
                return

            if self._stop_timer:
                self._stop_timer.cancel()

            logger.info(f"Motion ended, stopping recording in {self.post_roll}s")
            self._stop_timer = threading.Timer(self.post_roll, self._stop_recording)
            self._stop_timer.start()

    def _stop_recording(self, finalize_async: bool = True):
        """Stop the current recording.

        Stops the monitor thread, terminates ffmpeg, concatenates segments
        if multiple exist, and calculates the real duration using ffprobe.
        """
        with self._transition_lock:
            with self._lock:
                if not self._recording:
                    return
                self._recording = False
                self._monitor_stop_event.set()
                process = self._ffmpeg_process
                monitor_thread = self._monitor_thread
                recording = self._current_recording
                segment_files = list(self._segment_files)
                damaged_segments = set(self._damaged_segments)
                end_time = time.time()
                self._ffmpeg_process = None
                self._active_segment_path = None
                self._monitor_thread = None
                self._current_recording = None
                self._segment_files = []
                self._damaged_segments = set()
                self._stop_timer = None

            if process:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass

            if monitor_thread and monitor_thread.is_alive() and monitor_thread is not threading.current_thread():
                monitor_thread.join(timeout=5)

        if recording:
            recording.end_time = end_time
            # Finalization is deliberately detached from motion callbacks.
            # A new event can begin while concat/thumbnails are processed.
            if finalize_async:
                threading.Thread(
                    target=self._finalize_recording,
                    args=(recording, segment_files, damaged_segments),
                    daemon=True,
                ).start()
            else:
                self._finalize_recording(recording, segment_files, damaged_segments)

    def _finalize_recording(
        self,
        recording: Recording,
        segment_files: List[str],
        damaged_segments=None,
    ):
        """Build the final MP4 and metadata without blocking motion detection."""
        filepath = Path(recording.filepath)
        wall_clock_duration = (recording.end_time or time.time()) - recording.start_time
        success = False

        if len(segment_files) > 1:
            success = self._concatenate_segments(
                filepath, segment_files, damaged_segments=damaged_segments
            )
        elif len(segment_files) == 1:
            segment_path = Path(segment_files[0])
            if segment_path.exists() and segment_path.stat().st_size > 0:
                success = self._remux_segment(segment_path, filepath)

        if not success and filepath.exists():
            # A failed concat may leave a truncated destination. Source
            # segments are still intact, so discard only the invalid output.
            try:
                filepath.unlink()
            except OSError as e:
                logger.error(f"Failed to remove incomplete output: {e}")

        if not success and not filepath.exists():
            valid = [Path(p) for p in segment_files if Path(p).exists() and Path(p).stat().st_size > 0]
            largest = max(valid, key=lambda p: p.stat().st_size, default=None)
            if largest:
                try:
                    success = self._remux_segment(largest, filepath)
                    if not success:
                        raise OSError("ffmpeg could not remux the recovery segment")
                    logger.warning(f"Concat failed; recovered largest segment {largest.name}")
                except OSError as e:
                    logger.error(f"Failed to recover largest segment: {e}")

        if not success or not filepath.exists():
            logger.error(f"Recording file could not be finalized: {recording.filename}")
            return

        self._cleanup_segment_files(segment_files, keep=filepath)
        actual_duration = self._get_ffprobe_duration(filepath)
        recording.duration = actual_duration if actual_duration else wall_clock_duration
        recording.wall_clock_duration = wall_clock_duration
        recording.filesize = filepath.stat().st_size
        screenshots = self._generate_screenshots(filepath)
        recording.screenshots = screenshots
        if screenshots:
            recording.thumbnail = str(filepath.parent / screenshots[0])

        with self._lock:
            self._recordings.append(recording)
            self._save_metadata()
            self._cleanup_old_recordings()

        logger.info(
            f"Recording saved: {recording.filename} "
            f"(video={recording.duration:.1f}s, wall={wall_clock_duration:.1f}s, "
            f"segments={len(segment_files)}, {recording.filesize / 1024 / 1024:.1f}MB)"
        )

        if self.llm_analyzer and self.llm_auto_analyze and screenshots:
            self._trigger_llm_analysis(recording.filename, screenshots)

    def stop_recording_immediate(self):
        """Stop recording immediately without post-roll."""
        with self._lock:
            if self._stop_timer:
                self._stop_timer.cancel()
                self._stop_timer = None

        # Shutdown must wait for remux and metadata persistence; otherwise the
        # daemon finalizer is killed with the Python process.
        self._stop_recording(finalize_async=False)

    @property
    def is_recording(self) -> bool:
        """Check if currently recording."""
        return self._recording

    def get_recordings(self) -> List[Recording]:
        """Get list of all recordings."""
        return self._recordings.copy()

    def get_latest_recording(self) -> Optional[Recording]:
        """Get the most recent recording."""
        if self._recordings:
            return max(self._recordings, key=lambda r: r.start_time)
        return None

    def get_stats(self) -> dict:
        """Get recording statistics."""
        total_size = sum(r.filesize or 0 for r in self._recordings)
        return {
            "is_recording": self._recording,
            "total_recordings": len(self._recordings),
            "total_size_mb": total_size / 1024 / 1024,
            "latest_recording": self.get_latest_recording().filename if self.get_latest_recording() else None
        }


# For standalone testing
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.DEBUG,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    manager = RecordingManager(
        stream_url="http://192.168.1.156:8080/stream.m3u8",
        recordings_path="./test_recordings",
        pre_roll=6,
        post_roll=5,
        max_recordings=10
    )

    print("Starting test recording...")
    manager.start_recording()

    time.sleep(10)

    print("Scheduling stop...")
    manager.schedule_stop()

    time.sleep(10)

    stats = manager.get_stats()
    print(f"Stats: {stats}")
