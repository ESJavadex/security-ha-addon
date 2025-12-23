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
        pre_roll: int = 6,
        post_roll: int = 5,
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

        # Create recordings directory
        self.recordings_path.mkdir(parents=True, exist_ok=True)

        # Metadata file
        self.metadata_file = self.recordings_path / "recordings.json"
        self._recordings: List[Recording] = self._load_metadata()

    def _load_metadata(self) -> List[Recording]:
        """Load recording metadata from JSON file."""
        if self.metadata_file.exists():
            try:
                with open(self.metadata_file, 'r') as f:
                    data = json.load(f)
                    return [Recording(**r) for r in data]
            except Exception as e:
                logger.error(f"Error loading metadata: {e}")
        return []

    def _save_metadata(self):
        """Save recording metadata to JSON file."""
        try:
            with open(self.metadata_file, 'w') as f:
                json.dump([r.to_dict() for r in self._recordings], f, indent=2)
        except Exception as e:
            logger.error(f"Error saving metadata: {e}")

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

        # Get video duration using ffprobe
        try:
            probe_cmd = [
                'ffprobe',
                '-v', 'error',
                '-show_entries', 'format=duration',
                '-of', 'default=noprint_wrappers=1:nokey=1',
                str(video_path)
            ]
            result = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=30)
            duration = float(result.stdout.strip())
        except Exception as e:
            logger.warning(f"Could not get video duration: {e}, using 30s fallback")
            duration = 30.0

        # Generate screenshots at intervals
        timestamp = 1  # Start at 1 second
        index = 0

        while timestamp < duration:
            screenshot_name = f"{base_name}_{index:03d}.jpg"
            screenshot_path = video_path.parent / screenshot_name

            try:
                cmd = [
                    'ffmpeg',
                    '-i', str(video_path),
                    '-ss', str(timestamp),
                    '-vframes', '1',
                    '-vf', 'scale=320:-1',  # 320px width, preserve aspect
                    '-y',
                    '-loglevel', 'error',
                    str(screenshot_path)
                ]
                subprocess.run(cmd, timeout=30, check=True)
                screenshots.append(screenshot_name)
                logger.debug(f"Generated screenshot: {screenshot_name} at {timestamp}s")
            except Exception as e:
                logger.error(f"Error generating screenshot at {timestamp}s: {e}")

            timestamp += interval
            index += 1

        # If no screenshots were generated, try at least one at 1 second
        if not screenshots:
            screenshot_name = f"{base_name}_000.jpg"
            screenshot_path = video_path.parent / screenshot_name
            try:
                cmd = [
                    'ffmpeg',
                    '-i', str(video_path),
                    '-ss', '00:00:01',
                    '-vframes', '1',
                    '-vf', 'scale=320:-1',
                    '-y',
                    '-loglevel', 'error',
                    str(screenshot_path)
                ]
                subprocess.run(cmd, timeout=30, check=True)
                screenshots.append(screenshot_name)
            except Exception as e:
                logger.error(f"Error generating fallback screenshot: {e}")

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

    def start_recording(self, motion_start_time: Optional[float] = None):
        """
        Start recording video from stream.

        Args:
            motion_start_time: Timestamp when motion was first detected (for pre-roll)
        """
        with self._lock:
            if self._recording:
                logger.warning("Recording already in progress")
                # Cancel any pending stop
                if self._stop_timer:
                    self._stop_timer.cancel()
                    self._stop_timer = None
                return

            self._recording = True

            filename = self._generate_filename()
            filepath = self.recordings_path / filename

            self._current_recording = Recording(
                filename=filename,
                filepath=str(filepath),
                start_time=motion_start_time or time.time()
            )

            logger.info(f"Starting recording: {filename}")

            # Start ffmpeg to record stream
            cmd = [
                'ffmpeg',
                '-i', self.stream_url,
                '-c', 'copy',  # Copy codec, no transcoding
                '-movflags', '+faststart',  # Enable streaming
                '-t', str(self.max_duration),  # Max duration
                '-y',  # Overwrite
                '-loglevel', 'error',
                str(filepath)
            ]

            try:
                self._ffmpeg_process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE
                )
                logger.debug(f"ffmpeg started with PID {self._ffmpeg_process.pid}")
            except Exception as e:
                logger.error(f"Error starting ffmpeg: {e}")
                self._recording = False
                self._current_recording = None

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

    def _stop_recording(self):
        """Stop the current recording."""
        with self._lock:
            if not self._recording:
                return

            self._recording = False

            if self._ffmpeg_process:
                # Send SIGINT to ffmpeg for clean shutdown
                self._ffmpeg_process.terminate()
                try:
                    self._ffmpeg_process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self._ffmpeg_process.kill()
                self._ffmpeg_process = None

            if self._current_recording:
                self._current_recording.end_time = time.time()
                self._current_recording.duration = (
                    self._current_recording.end_time - self._current_recording.start_time
                )

                # Get file size
                filepath = Path(self._current_recording.filepath)
                if filepath.exists():
                    self._current_recording.filesize = filepath.stat().st_size

                    # Generate screenshots (every 5 seconds)
                    screenshots = self._generate_screenshots(filepath)
                    self._current_recording.screenshots = screenshots
                    # First screenshot is the thumbnail for backwards compatibility
                    if screenshots:
                        self._current_recording.thumbnail = str(filepath.parent / screenshots[0])

                    self._recordings.append(self._current_recording)
                    self._save_metadata()

                    logger.info(
                        f"Recording saved: {self._current_recording.filename} "
                        f"({self._current_recording.duration:.1f}s, "
                        f"{self._current_recording.filesize / 1024 / 1024:.1f}MB)"
                    )

                    # Trigger LLM analysis if auto-analyze is enabled
                    if self.llm_analyzer and self.llm_auto_analyze and screenshots:
                        self._trigger_llm_analysis(
                            self._current_recording.filename,
                            screenshots
                        )
                else:
                    logger.error(f"Recording file not found: {filepath}")

                self._current_recording = None

            # Cleanup old recordings
            self._cleanup_old_recordings()

    def stop_recording_immediate(self):
        """Stop recording immediately without post-roll."""
        with self._lock:
            if self._stop_timer:
                self._stop_timer.cancel()
                self._stop_timer = None

        self._stop_recording()

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
