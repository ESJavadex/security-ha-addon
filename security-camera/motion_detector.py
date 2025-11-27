#!/usr/bin/env python3
"""
Motion detector using OpenCV background subtraction.
Reads frames from HLS stream and detects motion changes.
"""

import cv2
import numpy as np
import subprocess
import time
import threading
import logging
import json
import os
from dataclasses import dataclass
from typing import Callable, Optional
from enum import Enum

logger = logging.getLogger(__name__)


class MotionState(Enum):
    IDLE = "idle"
    DETECTING = "detecting"  # Motion detected, waiting for min_duration
    ACTIVE = "active"        # Motion confirmed (exceeded min_duration)


@dataclass
class MotionEvent:
    """Represents a motion detection event."""
    timestamp: float
    motion_area: int
    frame: Optional[np.ndarray] = None


class MotionDetector:
    """
    Detects motion in HLS video stream using OpenCV MOG2 background subtraction.

    Features:
    - Reads directly from m3u8 stream
    - Configurable motion threshold
    - Time-based filtering (motion must persist for min_duration)
    - Callbacks for motion start/end events
    """

    def __init__(
        self,
        stream_url: str,
        motion_threshold: int = 5000,
        min_duration: float = 3.0,
        check_interval: float = 1.0,
        roi_x_start: int = 0,
        roi_x_end: int = 100,
        roi_y_start: int = 0,
        roi_y_end: int = 100,
        settings_file: Optional[str] = None,
        on_motion_start: Optional[Callable[[MotionEvent], None]] = None,
        on_motion_end: Optional[Callable[[MotionEvent], None]] = None,
        on_motion_frame: Optional[Callable[[MotionEvent], None]] = None,
    ):
        """
        Initialize motion detector.

        Args:
            stream_url: HLS stream URL (m3u8)
            motion_threshold: Minimum contour area to consider as motion
            min_duration: Seconds motion must persist before triggering
            check_interval: Seconds between frame checks
            roi_x_start: Left boundary of detection zone (0-100 percentage)
            roi_x_end: Right boundary of detection zone (0-100 percentage)
            roi_y_start: Top boundary of detection zone (0-100 percentage)
            roi_y_end: Bottom boundary of detection zone (0-100 percentage)
            settings_file: Path to JSON settings file for live updates
            on_motion_start: Callback when motion confirmed
            on_motion_end: Callback when motion ends
            on_motion_frame: Callback for each frame with motion
        """
        self.stream_url = stream_url
        self.motion_threshold = motion_threshold
        self.min_duration = min_duration
        self.check_interval = check_interval
        self.roi_x_start = max(0, min(100, roi_x_start))
        self.roi_x_end = max(0, min(100, roi_x_end))
        self.roi_y_start = max(0, min(100, roi_y_start))
        self.roi_y_end = max(0, min(100, roi_y_end))
        self.settings_file = settings_file
        self._settings_mtime = 0

        self.on_motion_start = on_motion_start
        self.on_motion_end = on_motion_end
        self.on_motion_frame = on_motion_frame

        # Background subtractor (MOG2 is good balance of accuracy/speed)
        self.bg_subtractor = cv2.createBackgroundSubtractorMOG2(
            history=500,
            varThreshold=50,
            detectShadows=False  # Disable shadow detection for speed
        )

        # State tracking
        self.state = MotionState.IDLE
        self.motion_start_time: Optional[float] = None
        self.last_motion_time: Optional[float] = None
        self.motion_cooldown = 2.0  # Seconds without motion before ending

        # Thread control
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

        # Stats
        self.frames_processed = 0
        self.motion_events = 0

    def _extract_frame(self) -> Optional[np.ndarray]:
        """Extract a single frame from the HLS stream using ffmpeg."""
        try:
            cmd = [
                'ffmpeg',
                '-i', self.stream_url,
                '-vframes', '1',
                '-f', 'image2pipe',
                '-pix_fmt', 'bgr24',
                '-vcodec', 'rawvideo',
                '-loglevel', 'error',
                '-'
            ]

            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=10
            )

            if result.returncode != 0:
                logger.warning(f"ffmpeg error: {result.stderr.decode()}")
                return None

            # Decode raw frame data
            # We need to know dimensions - try common resolutions
            raw_data = result.stdout

            # Try to decode as various resolutions
            for width, height in [(1920, 1080), (1280, 720), (640, 480), (640, 360)]:
                expected_size = width * height * 3  # BGR = 3 bytes per pixel
                if len(raw_data) >= expected_size:
                    frame = np.frombuffer(raw_data[:expected_size], dtype=np.uint8)
                    frame = frame.reshape((height, width, 3))
                    return frame

            logger.warning(f"Unexpected frame size: {len(raw_data)} bytes")
            return None

        except subprocess.TimeoutExpired:
            logger.warning("Frame extraction timed out")
            return None
        except Exception as e:
            logger.error(f"Frame extraction error: {e}")
            return None

    def _extract_frame_cv2(self) -> Optional[np.ndarray]:
        """Extract frame using OpenCV directly (alternative method)."""
        try:
            cap = cv2.VideoCapture(self.stream_url)
            if not cap.isOpened():
                logger.warning("Could not open stream with OpenCV")
                return None

            ret, frame = cap.read()
            cap.release()

            if not ret:
                logger.warning("Could not read frame from stream")
                return None

            return frame

        except Exception as e:
            logger.error(f"OpenCV frame extraction error: {e}")
            return None

    def _detect_motion(self, frame: np.ndarray) -> int:
        """
        Detect motion in frame using background subtraction.
        Only analyzes the middle third of the frame vertically to reduce
        false positives from lighting changes on the sides.

        Returns:
            Total area of motion contours (0 if no motion)
        """
        # Crop to region of interest (ROI) - configurable detection zone
        height, width = frame.shape[:2]
        left_bound = int(width * self.roi_x_start / 100)
        right_bound = int(width * self.roi_x_end / 100)
        top_bound = int(height * self.roi_y_start / 100)
        bottom_bound = int(height * self.roi_y_end / 100)
        roi_frame = frame[top_bound:bottom_bound, left_bound:right_bound]

        logger.debug(f"ROI: x={left_bound}-{right_bound}, y={top_bound}-{bottom_bound}")

        # Resize for faster processing (optional, reduces CPU load)
        scale = 0.5
        small_frame = cv2.resize(roi_frame, None, fx=scale, fy=scale)

        # Apply background subtraction
        fg_mask = self.bg_subtractor.apply(small_frame)

        # Clean up mask
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        fg_mask = cv2.morphologyEx(fg_mask, cv2.MORPH_OPEN, kernel)
        fg_mask = cv2.morphologyEx(fg_mask, cv2.MORPH_CLOSE, kernel)

        # Find contours
        contours, _ = cv2.findContours(
            fg_mask,
            cv2.RETR_EXTERNAL,
            cv2.CHAIN_APPROX_SIMPLE
        )

        # Calculate total motion area
        total_area = sum(cv2.contourArea(c) for c in contours)

        # Scale back to original frame size
        total_area = int(total_area / (scale * scale))

        return total_area

    def _process_frame(self):
        """Process a single frame and update motion state."""
        # Try OpenCV first (usually works better with HLS)
        frame = self._extract_frame_cv2()
        if frame is None:
            # Fallback to ffmpeg
            frame = self._extract_frame()

        if frame is None:
            logger.debug("No frame available")
            return

        self.frames_processed += 1

        # Detect motion
        motion_area = self._detect_motion(frame)
        current_time = time.time()
        has_motion = motion_area > self.motion_threshold

        logger.debug(f"Motion area: {motion_area}, threshold: {self.motion_threshold}, has_motion: {has_motion}")

        with self._lock:
            if has_motion:
                self.last_motion_time = current_time

                if self.state == MotionState.IDLE:
                    # Start detecting
                    self.state = MotionState.DETECTING
                    self.motion_start_time = current_time
                    logger.info(f"Motion detected, waiting for {self.min_duration}s confirmation")

                elif self.state == MotionState.DETECTING:
                    # Check if min_duration exceeded
                    elapsed = current_time - self.motion_start_time
                    if elapsed >= self.min_duration:
                        self.state = MotionState.ACTIVE
                        self.motion_events += 1
                        logger.info(f"Motion confirmed! Event #{self.motion_events}")

                        if self.on_motion_start:
                            event = MotionEvent(
                                timestamp=self.motion_start_time,
                                motion_area=motion_area,
                                frame=frame.copy()
                            )
                            self.on_motion_start(event)

                elif self.state == MotionState.ACTIVE:
                    # Continue active motion
                    if self.on_motion_frame:
                        event = MotionEvent(
                            timestamp=current_time,
                            motion_area=motion_area,
                            frame=frame.copy()
                        )
                        self.on_motion_frame(event)

            else:
                # No motion detected
                if self.state == MotionState.DETECTING:
                    # Motion stopped before min_duration - reset
                    logger.debug("Motion stopped before confirmation, resetting")
                    self.state = MotionState.IDLE
                    self.motion_start_time = None

                elif self.state == MotionState.ACTIVE:
                    # Check cooldown
                    if self.last_motion_time:
                        elapsed = current_time - self.last_motion_time
                        if elapsed >= self.motion_cooldown:
                            logger.info(f"Motion ended after {self.motion_cooldown}s cooldown")
                            self.state = MotionState.IDLE

                            if self.on_motion_end:
                                event = MotionEvent(
                                    timestamp=current_time,
                                    motion_area=0
                                )
                                self.on_motion_end(event)

                            self.motion_start_time = None
                            self.last_motion_time = None

    def _run_loop(self):
        """Main detection loop."""
        logger.info(f"Starting motion detection on {self.stream_url}")
        logger.info(f"Threshold: {self.motion_threshold}, Min duration: {self.min_duration}s")
        logger.info(f"ROI: {self.roi_x_start}% - {self.roi_x_end}%")
        if self.settings_file:
            logger.info(f"Settings file: {self.settings_file} (live reload enabled)")

        while self._running:
            try:
                # Check for settings file changes
                self._reload_settings_from_file()
                self._process_frame()
            except Exception as e:
                logger.error(f"Error processing frame: {e}")

            time.sleep(self.check_interval)

        logger.info("Motion detection stopped")

    def start(self):
        """Start motion detection in background thread."""
        if self._running:
            logger.warning("Motion detector already running")
            return

        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self):
        """Stop motion detection."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    @property
    def is_motion_active(self) -> bool:
        """Check if motion is currently active."""
        with self._lock:
            return self.state == MotionState.ACTIVE

    def set_roi(self, x_start: int, x_end: int, y_start: int = None, y_end: int = None):
        """
        Update the region of interest for motion detection.

        Args:
            x_start: Left boundary (0-100 percentage)
            x_end: Right boundary (0-100 percentage)
            y_start: Top boundary (0-100 percentage)
            y_end: Bottom boundary (0-100 percentage)
        """
        with self._lock:
            self.roi_x_start = max(0, min(100, x_start))
            self.roi_x_end = max(0, min(100, x_end))
            if y_start is not None:
                self.roi_y_start = max(0, min(100, y_start))
            if y_end is not None:
                self.roi_y_end = max(0, min(100, y_end))
            logger.info(f"ROI updated: x={self.roi_x_start}%-{self.roi_x_end}%, y={self.roi_y_start}%-{self.roi_y_end}%")

    def set_threshold(self, threshold: int):
        """Update motion threshold dynamically."""
        with self._lock:
            self.motion_threshold = max(0, threshold)
            logger.info(f"Motion threshold updated: {self.motion_threshold}")

    def _reload_settings_from_file(self):
        """Check settings file for changes and reload if modified."""
        if not self.settings_file:
            return

        try:
            if not os.path.exists(self.settings_file):
                return

            mtime = os.path.getmtime(self.settings_file)
            if mtime <= self._settings_mtime:
                return  # File hasn't changed

            with open(self.settings_file, 'r') as f:
                settings = json.load(f)

            self._settings_mtime = mtime

            # Apply settings
            changed = False
            if 'roi_x_start' in settings and settings['roi_x_start'] != self.roi_x_start:
                self.roi_x_start = max(0, min(100, int(settings['roi_x_start'])))
                changed = True
            if 'roi_x_end' in settings and settings['roi_x_end'] != self.roi_x_end:
                self.roi_x_end = max(0, min(100, int(settings['roi_x_end'])))
                changed = True
            if 'roi_y_start' in settings and settings['roi_y_start'] != self.roi_y_start:
                self.roi_y_start = max(0, min(100, int(settings['roi_y_start'])))
                changed = True
            if 'roi_y_end' in settings and settings['roi_y_end'] != self.roi_y_end:
                self.roi_y_end = max(0, min(100, int(settings['roi_y_end'])))
                changed = True
            if 'motion_threshold' in settings and settings['motion_threshold'] != self.motion_threshold:
                self.motion_threshold = max(0, int(settings['motion_threshold']))
                changed = True

            if changed:
                logger.info(f"Settings reloaded: ROI x={self.roi_x_start}%-{self.roi_x_end}%, y={self.roi_y_start}%-{self.roi_y_end}%, threshold={self.motion_threshold}")

        except Exception as e:
            logger.warning(f"Error reloading settings: {e}")

    def get_stats(self) -> dict:
        """Get detection statistics."""
        return {
            "frames_processed": self.frames_processed,
            "motion_events": self.motion_events,
            "state": self.state.value,
            "is_motion_active": self.is_motion_active,
            "roi_x_start": self.roi_x_start,
            "roi_x_end": self.roi_x_end,
            "roi_y_start": self.roi_y_start,
            "roi_y_end": self.roi_y_end,
            "motion_threshold": self.motion_threshold
        }


# For standalone testing
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.DEBUG,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    def on_start(event: MotionEvent):
        print(f">>> MOTION STARTED at {event.timestamp}, area: {event.motion_area}")

    def on_end(event: MotionEvent):
        print(f">>> MOTION ENDED at {event.timestamp}")

    detector = MotionDetector(
        stream_url="http://localhost:8080/stream.m3u8",
        motion_threshold=5000,
        min_duration=3.0,
        check_interval=1.0,
        on_motion_start=on_start,
        on_motion_end=on_end
    )

    detector.start()

    try:
        while True:
            time.sleep(5)
            stats = detector.get_stats()
            print(f"Stats: {stats}")
    except KeyboardInterrupt:
        detector.stop()
