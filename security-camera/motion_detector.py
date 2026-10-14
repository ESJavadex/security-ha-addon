#!/usr/bin/env python3
"""
Motion and person detector for an HLS camera stream.

Pipeline (one persistent ffmpeg reader, a few frames per second):

  1. Light / exposure filter. The camera watches a hallway whose lights switch
     on and off with a timer and whose exposure readjusts afterwards. Those
     global changes used to be ~80 % of all recordings. When the mean
     brightness jumps or most of the frame changes at once, the background
     model is rebuilt and motion is ignored for a short settle period.
  2. Background subtraction (MOG2) on the detection zone, scored by the
     largest connected blob instead of the sum of every noisy pixel.
  3. Person detection (YOLOX, optional) on the frames that matter: while there
     is motion, after the light turns on, during an event, and as a periodic
     check. A confirmed person starts an event immediately.
  4. Events: "person" when a person is confirmed; "motion" only for localized
     motion that keeps going for `motion_fallback` seconds (0 disables it).
"""

import json
import logging
import os
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Deque, List, Optional, Tuple

import cv2
import numpy as np

logger = logging.getLogger(__name__)

# Working resolution for all analysis (the stream is 1080p).
WORK_W, WORK_H = 640, 360
# Motion analysis runs on a further downscaled copy.
MOTION_SCALE = 0.5
# Reference frame the legacy `motion_threshold` (pixel area) was calibrated on.
LEGACY_REF_PIXELS = 1920 * 1080


class MotionState(Enum):
    IDLE = "idle"
    DETECTING = "detecting"  # Something is going on, not confirmed yet
    ACTIVE = "active"        # Event confirmed (recording)


@dataclass
class MotionEvent:
    """Represents a motion/person event."""
    timestamp: float
    motion_area: int
    frame: Optional[np.ndarray] = None
    trigger: Optional[str] = None          # "person" | "motion"
    person_score: Optional[float] = None
    person_boxes: List[Tuple[int, int, int, int]] = field(default_factory=list)
    snapshot: Optional[np.ndarray] = None  # frame with the person boxed


class FrameReader:
    """
    Keeps one ffmpeg process reading the HLS stream and decoding `fps` frames
    per second at WORK_W x WORK_H. Restarts ffmpeg when it exits or stalls.
    Only the most recent frames are kept: if analysis falls behind, old frames
    are dropped instead of queueing latency.
    """

    def __init__(self, stream_url: str, fps: float = 4.0, stall_timeout: float = 20.0):
        self.stream_url = stream_url
        self.fps = fps
        self.stall_timeout = stall_timeout
        self._frames: Deque[Tuple[float, np.ndarray]] = deque(maxlen=int(max(2, fps * 3)))
        self._cond = threading.Condition()
        self._running = False
        self._proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._watchdog: Optional[threading.Thread] = None
        self._last_frame_time = 0.0
        self.frames_read = 0
        self.restarts = 0

    def _cmd(self) -> List[str]:
        return [
            'ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin',
            '-live_start_index', '-1',
            '-rw_timeout', '15000000',
            '-i', self.stream_url,
            '-an', '-sn',
            '-vf', f'fps={self.fps},scale={WORK_W}:{WORK_H}',
            '-pix_fmt', 'bgr24', '-f', 'rawvideo', '-',
        ]

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="frame-reader")
        self._thread.start()
        self._watchdog = threading.Thread(target=self._watch, daemon=True, name="frame-watchdog")
        self._watchdog.start()

    def stop(self):
        self._running = False
        self._kill()
        with self._cond:
            self._cond.notify_all()

    def _kill(self):
        proc = self._proc
        if proc and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass

    def _watch(self):
        while self._running:
            time.sleep(2)
            if self._proc and self._proc.poll() is None and self._last_frame_time:
                if time.time() - self._last_frame_time > self.stall_timeout:
                    logger.warning(f"No frames for {self.stall_timeout:.0f}s, restarting stream reader")
                    self._last_frame_time = time.time()
                    self._kill()

    def _run(self):
        frame_bytes = WORK_W * WORK_H * 3
        backoff = 1.0
        while self._running:
            started = time.time()
            try:
                self._proc = subprocess.Popen(
                    self._cmd(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=frame_bytes
                )
            except OSError as e:
                logger.error(f"Could not start ffmpeg reader: {e}")
                time.sleep(5)
                continue
            self._last_frame_time = time.time()
            logger.info("Stream reader started")
            stdout = self._proc.stdout
            while self._running:
                buf = stdout.read(frame_bytes)
                if not buf or len(buf) < frame_bytes:
                    break
                frame = np.frombuffer(buf, dtype=np.uint8).reshape((WORK_H, WORK_W, 3))
                now = time.time()
                self._last_frame_time = now
                self.frames_read += 1
                with self._cond:
                    self._frames.append((now, frame))
                    self._cond.notify()
            err = b''
            try:
                self._kill()
                _, err = self._proc.communicate(timeout=5)
            except Exception:
                pass
            if not self._running:
                break
            self.restarts += 1
            msg = (err or b'').decode(errors='replace').strip().splitlines()[-1:] or ['']
            logger.warning(f"Stream reader ended (restart #{self.restarts}): {msg[0]}")
            # Quick restart after a long healthy run, back off on repeated failures
            backoff = 1.0 if time.time() - started > 60 else min(backoff * 2, 30.0)
            time.sleep(backoff)

    def get(self, timeout: float = 5.0) -> Optional[Tuple[float, np.ndarray]]:
        """Return the oldest pending frame (dropping a backlog), or None on timeout."""
        with self._cond:
            if not self._frames:
                self._cond.wait(timeout)
            if not self._frames:
                return None
            # If we fell behind, skip to the most recent couple of frames
            while len(self._frames) > 2:
                self._frames.popleft()
            return self._frames.popleft()


class MotionDetector:
    """
    Detects people and real motion in an HLS stream, filtering light changes.
    Public API kept compatible with the previous MOG2-only detector.
    """

    def __init__(
        self,
        stream_url: str,
        motion_threshold: int = 5000,
        min_duration: float = 5.0,
        check_interval: float = 1.0,
        motion_cooldown: float = 10.0,
        roi_x_start: int = 0,
        roi_x_end: int = 100,
        roi_y_start: int = 0,
        roi_y_end: int = 100,
        settings_file: Optional[str] = None,
        on_motion_start: Optional[Callable[[MotionEvent], None]] = None,
        on_motion_end: Optional[Callable[[MotionEvent], None]] = None,
        on_motion_frame: Optional[Callable[[MotionEvent], None]] = None,
        fps: float = 4.0,
        person_model: Optional[str] = None,
        person_score: float = 0.5,
        person_input_size: int = 416,
        person_interval: float = 0.5,
        person_idle_interval: float = 5.0,
        motion_fallback: float = 8.0,
        light_settle: float = 3.0,
        dark_level: float = 18.0,
        on_person: Optional[Callable[[MotionEvent], None]] = None,
    ):
        """
        Args (legacy):
            motion_threshold: Minimum blob area (pixels at 1080p) to count as motion
            min_duration: Kept for compatibility; with person detection disabled it is
                the time localized motion must persist (same meaning as before)
            check_interval: Unused (the reader runs at `fps`)
            motion_cooldown: Seconds without person/motion before ending the event
            roi_*: Detection zone in percent
        Args (new):
            fps: Frames analysed per second
            person_model: Path to the YOLOX ONNX model; None disables person detection
            person_score: Detector score needed for a person hit
            person_input_size: Network input size (416 is enough for this camera)
            person_interval: Min seconds between detector runs while something happens
            person_idle_interval: Periodic detector run when the scene is lit and quiet
            motion_fallback: Seconds of localized motion without a person that still
                start a "motion" event (0 = record only people)
            light_settle: Seconds motion is ignored after a light/exposure change
            dark_level: Mean brightness below which the scene counts as dark
        """
        self.stream_url = stream_url
        self.motion_threshold = motion_threshold
        self.min_duration = min_duration
        self.check_interval = check_interval
        self.motion_cooldown = motion_cooldown
        self.roi_x_start = max(0, min(100, roi_x_start))
        self.roi_x_end = max(0, min(100, roi_x_end))
        self.roi_y_start = max(0, min(100, roi_y_start))
        self.roi_y_end = max(0, min(100, roi_y_end))
        self.settings_file = settings_file
        self._settings_mtime = 0

        self.on_motion_start = on_motion_start
        self.on_motion_end = on_motion_end
        self.on_motion_frame = on_motion_frame
        self.on_person = on_person

        self.fps = fps
        self.person_score = person_score
        self.person_interval = person_interval
        self.person_idle_interval = person_idle_interval
        self.light_settle = light_settle
        self.dark_level = dark_level
        self.person_model = person_model
        self.person_input_size = person_input_size
        self._person_detector = None
        # Without a person detector, fall back to the legacy behaviour:
        # localized motion for min_duration starts an event.
        self.motion_fallback = motion_fallback if person_model else (motion_fallback or min_duration)

        self._reader = FrameReader(stream_url, fps=fps)
        self._bg = None
        self._prev_gray: Optional[np.ndarray] = None
        self._brightness_ema: Optional[float] = None
        self._brightness_hist: Deque[Tuple[float, float]] = deque()
        self._ref_small: Optional[np.ndarray] = None
        self._blob_center: Optional[Tuple[float, float]] = None
        # Fraction of the zone the motion blob must travel for a motion-only event
        self.motion_travel = 0.12
        self._ref_time = 0.0

        # State tracking
        self.state = MotionState.IDLE
        self.motion_start_time: Optional[float] = None
        self.last_motion_time: Optional[float] = None
        self.event_trigger: Optional[str] = None
        self._settle_until = 0.0
        self._light_on_at = 0.0
        self._last_person_run = 0.0
        self._person_hits: Deque[Tuple[float, float]] = deque()
        self._motion_window: Deque[Tuple[float, bool]] = deque()
        self._last_person_time = 0.0
        self._event_best_score = 0.0

        # Thread control
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

        # Stats
        self.frames_processed = 0
        self.motion_events = 0
        self.person_events = 0
        self.light_changes = 0
        self.camera_moves = 0
        self.person_runs = 0
        self.last_person_score = 0.0
        self.last_motion_area = 0
        self.brightness = 0.0
        self.scene = "unknown"
        self._person_ms = 0.0

    # ── Setup ──────────────────────────────────────────────────────────

    def _new_background(self):
        # history in frames: ~75 s at 4 fps. Shadows marked (127) and dropped.
        bg = cv2.createBackgroundSubtractorMOG2(history=300, varThreshold=40, detectShadows=True)
        bg.setShadowThreshold(0.6)
        return bg

    def _load_person_detector(self):
        if not self.person_model:
            logger.info("Person detection disabled (no model configured)")
            return
        if not os.path.exists(self.person_model):
            logger.error(f"Person model not found: {self.person_model}; running motion-only")
            self.motion_fallback = self.motion_fallback or self.min_duration
            return
        try:
            from person_detector import PersonDetector
            self._person_detector = PersonDetector(
                self.person_model,
                input_size=self.person_input_size,
                score_threshold=min(0.35, self.person_score),
                threads=1,
            )
        except Exception as e:
            logger.error(f"Could not load person detector: {e}; running motion-only")
            self.motion_fallback = self.motion_fallback or self.min_duration

    # ── Analysis ───────────────────────────────────────────────────────

    def _roi_bounds(self, w: int, h: int) -> Tuple[int, int, int, int]:
        x1 = int(w * self.roi_x_start / 100)
        x2 = int(w * self.roi_x_end / 100)
        y1 = int(h * self.roi_y_start / 100)
        y2 = int(h * self.roi_y_end / 100)
        if x2 - x1 < 8 or y2 - y1 < 8:
            return 0, 0, w, h
        return x1, y1, x2, y2

    def _min_blob_area(self, roi_w: int, roi_h: int) -> float:
        """Convert the legacy 1080p pixel threshold into the motion working resolution."""
        scale = (WORK_W * MOTION_SCALE) * (WORK_H * MOTION_SCALE) / LEGACY_REF_PIXELS
        return max(30.0, self.motion_threshold * scale)

    @staticmethod
    def _global_motion(a: np.ndarray, b: np.ndarray, min_shift: float) -> Tuple[bool, float, float]:
        """
        Track corner features from `a` to `b`. If most of them move, the camera
        moved (door opening, slow drift, fisheye re-framing). A person only
        moves the features on their body.
        """
        pts = cv2.goodFeaturesToTrack(a, maxCorners=120, qualityLevel=0.01, minDistance=7)
        if pts is None or len(pts) < 15:
            return False, 0.0, 0.0
        nxt, status, _ = cv2.calcOpticalFlowPyrLK(a, b, pts, None, winSize=(15, 15), maxLevel=2)
        ok = status.reshape(-1) == 1
        if ok.sum() < 15:
            # Lost track of almost everything: the picture changed completely
            return True, 1.0, 0.0
        disp = np.linalg.norm((nxt - pts).reshape(-1, 2)[ok], axis=1)
        frac = float(np.mean(disp > min_shift))
        return frac > 0.5, frac, float(np.median(disp))

    def _analyse_motion(self, frame: np.ndarray, now: float) -> Tuple[bool, int, bool]:
        """
        Returns (has_motion, blob_area_1080p_equiv, light_change).
        """
        x1, y1, x2, y2 = self._roi_bounds(frame.shape[1], frame.shape[0])
        gray = cv2.cvtColor(frame[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY)
        small = cv2.resize(gray, None, fx=MOTION_SCALE, fy=MOTION_SCALE, interpolation=cv2.INTER_AREA)
        small = cv2.GaussianBlur(small, (5, 5), 0)

        brightness = float(small.mean())
        self.brightness = brightness
        prev = self._prev_gray
        self._prev_gray = small

        light_change = False
        if prev is not None and prev.shape == small.shape:
            # The camera sits in a door peephole: opening or closing that door
            # shifts the whole picture. A global translation is camera motion,
            # not something moving in the scene.
            # Slow door movements drift less than a pixel per frame, so the
            # shift is also measured against a reference frame.
            if brightness >= self.dark_level:
                moved, frac, shift = self._global_motion(prev, small, 1.0)
                if not moved and self._ref_small is not None and self._ref_small.shape == small.shape:
                    moved, frac, shift = self._global_motion(self._ref_small, small, 2.0)
                if moved:
                    self.camera_moves += 1
                    self._bg = self._new_background()
                    self._bg.apply(small, learningRate=1.0)
                    self._ref_small, self._ref_time = small, now
                    self._settle_until = now + self.light_settle
                    self._motion_window.clear()
                    logger.info(f"Camera moved ({frac:.0%} of points, median {shift:.1f}px), rebuilding background")
                    return False, 0, True
            prev_b = float(prev.mean())
            diff = cv2.absdiff(small, prev)
            changed = float(np.count_nonzero(diff > 25)) / diff.size
            jump = abs(brightness - prev_b)
            rel = jump / max(prev_b, 8.0)
            # Lights on/off or exposure step: big global jump, or most of the
            # frame changing at once (a person close to the lens changes a large
            # area too, but not the global brightness by this much).
            if (jump > 12 and rel > 0.15) or jump > 30 or (changed > 0.6 and jump > 6):
                light_change = True
                if brightness > prev_b and prev_b < self.dark_level + 10:
                    self._light_on_at = now
                logger.info(f"Light/exposure change: brightness {prev_b:.0f} -> {brightness:.0f}, changed {changed:.0%}")

        if self._bg is None or light_change:
            self._bg = self._new_background()
            self._bg.apply(small, learningRate=1.0)
            self._ref_small, self._ref_time = small, now
            if light_change:
                self.light_changes += 1
                self._settle_until = now + self.light_settle
            return False, 0, light_change

        # Auto-exposure ramps after a light change: brightness keeps drifting
        # for several seconds. While it drifts, keep learning fast and do not
        # count motion (person detection still runs).
        self._brightness_hist.append((now, brightness))
        while self._brightness_hist and now - self._brightness_hist[0][0] > 1.0:
            self._brightness_hist.popleft()
        if len(self._brightness_hist) >= 3:
            drift = abs(self._brightness_hist[-1][1] - self._brightness_hist[0][1])
            if drift > 3.0:
                self._settle_until = max(self._settle_until, now + 1.0)

        settling = now < self._settle_until
        # Learn fast while exposure settles after a light change
        mask = self._bg.apply(small, learningRate=0.2 if settling else -1)
        if settling or brightness < self.dark_level:
            return False, 0, False

        _, mask = cv2.threshold(mask, 200, 255, cv2.THRESH_BINARY)  # drop shadows
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
        mask = cv2.dilate(mask, kernel, iterations=2)

        fg_ratio = float(np.count_nonzero(mask)) / mask.size
        if fg_ratio > 0.55:
            # Whole frame "moving" without a brightness jump: auto-exposure /
            # white balance drift or camera shake. Rebuild and settle.
            self._bg = self._new_background()
            self._bg.apply(small, learningRate=1.0)
            self._settle_until = now + self.light_settle
            self.light_changes += 1
            logger.info(f"Global change ({fg_ratio:.0%} of frame), treating as exposure change")
            return False, 0, True

        if fg_ratio < 0.002 and now - self._ref_time > 10.0:
            # Quiet scene: refresh the camera-position reference
            self._ref_small, self._ref_time = small, now

        n, _, stats, centroids = cv2.connectedComponentsWithStats(mask, connectivity=8)
        largest = 0
        self._blob_center = None
        if n > 1:
            k = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
            largest = int(stats[k, cv2.CC_STAT_AREA])
            cx, cy = centroids[k]
            self._blob_center = (cx / mask.shape[1], cy / mask.shape[0])
        min_area = self._min_blob_area(small.shape[1], small.shape[0])
        legacy_area = int(largest * LEGACY_REF_PIXELS / ((WORK_W * MOTION_SCALE) * (WORK_H * MOTION_SCALE)))
        return largest >= min_area, legacy_area, False

    def _should_run_person(self, now: float, has_motion: bool) -> bool:
        if self._person_detector is None:
            return False
        if self.brightness < self.dark_level and now - self._light_on_at > 5:
            return False  # nothing visible in the dark
        since = now - self._last_person_run
        busy = (
            has_motion
            or self.state != MotionState.IDLE
            or now - self._light_on_at < 60
            or now - self._last_person_time < 10
            or now < self._settle_until
        )
        return since >= (self.person_interval if busy else self.person_idle_interval)

    def _run_person(self, frame: np.ndarray, now: float):
        self._last_person_run = now
        t0 = time.time()
        try:
            dets = self._person_detector.detect(frame)
        except Exception as e:
            logger.error(f"Person detection failed: {e}")
            return [], 0.0
        self._person_ms = (time.time() - t0) * 1000
        self.person_runs += 1

        x1, y1, x2, y2 = self._roi_bounds(frame.shape[1], frame.shape[0])
        min_area = 0.008 * frame.shape[0] * frame.shape[1]
        keep = []
        for d in dets:
            bx1, by1, bx2, by2 = d.box
            cx, cy = (bx1 + bx2) / 2, (by1 + by2) / 2
            if d.area < min_area or not (x1 <= cx <= x2 and y1 <= cy <= y2):
                continue
            keep.append(d)
        best = max((d.score for d in keep), default=0.0)
        self.last_person_score = best
        if best >= self.person_score:
            self._person_hits.append((now, best))
            self._last_person_time = now
            logger.debug(f"Person hit {best:.2f} ({self._person_ms:.0f} ms)")
        return keep, best

    def _person_confirmed(self, now: float) -> bool:
        while self._person_hits and now - self._person_hits[0][0] > 4.0:
            self._person_hits.popleft()
        if not self._person_hits:
            return False
        strong = any(s >= max(0.75, self.person_score) for _, s in self._person_hits)
        return strong or len(self._person_hits) >= 2

    def _sustained_motion(self, now: float, has_motion: bool) -> bool:
        if self.motion_fallback <= 0:
            return False
        center = self._blob_center if has_motion else None
        self._motion_window.append((now, has_motion, center))
        while self._motion_window and now - self._motion_window[0][0] > self.motion_fallback:
            self._motion_window.popleft()
        if not self._motion_window or now - self._motion_window[0][0] < self.motion_fallback * 0.9:
            return False
        # Blobs glued to the zone border are lens-edge glare and door-frame
        # flicker; something walking around ends up inside the zone.
        hits = [c for _, m, c in self._motion_window
                if m and c is not None and 0.05 < c[0] < 0.95 and 0.05 < c[1] < 0.95]
        if len(hits) < 0.6 * len(self._motion_window):
            return False
        # Something real moves around; flicker or noise stays in one spot
        xs, ys = [c[0] for c in hits], [c[1] for c in hits]
        return max(max(xs) - min(xs), max(ys) - min(ys)) >= self.motion_travel

    @staticmethod
    def _snapshot(frame: np.ndarray, dets) -> np.ndarray:
        snap = frame.copy()
        for d in dets:
            x1, y1, x2, y2 = d.box
            cv2.rectangle(snap, (x1, y1), (x2, y2), (0, 220, 0), 2)
            cv2.putText(snap, f"persona {d.score:.2f}", (x1 + 3, max(14, y1 + 16)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 220, 0), 1, cv2.LINE_AA)
        return snap

    def _process(self, ts: float, frame: np.ndarray):
        now = ts
        self.frames_processed += 1
        has_motion, area, light_change = self._analyse_motion(frame, now)
        self.last_motion_area = area
        self.scene = "dark" if self.brightness < self.dark_level else "lit"

        dets, score = [], 0.0
        if self._should_run_person(now, has_motion):
            dets, score = self._run_person(frame, now)
        person_now = score >= self.person_score
        confirmed = self._person_confirmed(now)
        sustained = self._sustained_motion(now, has_motion)

        start_event = end_event = None
        frame_event = None
        with self._lock:
            active_signal = person_now or (has_motion and self.state == MotionState.ACTIVE)
            if person_now or has_motion:
                self.last_motion_time = now

            if self.state != MotionState.ACTIVE:
                if confirmed or sustained:
                    trigger = "person" if confirmed else "motion"
                    self.state = MotionState.ACTIVE
                    self.event_trigger = trigger
                    self.motion_events += 1
                    if trigger == "person":
                        self.person_events += 1
                        first = self._person_hits[0][0] if self._person_hits else now
                        self.motion_start_time = min(self.motion_start_time or first, first)
                    else:
                        self.motion_start_time = self.motion_start_time or (now - self.motion_fallback)
                    self.last_motion_time = now
                    self._event_best_score = score if person_now else 0.0
                    logger.info(f"Event #{self.motion_events} confirmed: {trigger}"
                                + (f" (score {score:.2f})" if person_now else f" (area {area})"))
                    start_event = MotionEvent(
                        timestamp=self.motion_start_time, motion_area=area, frame=frame,
                        trigger=trigger,
                        person_score=score if person_now else None,
                        person_boxes=[d.box for d in dets],
                        snapshot=self._snapshot(frame, dets) if person_now else None,
                    )
                elif has_motion or person_now:
                    if self.state == MotionState.IDLE:
                        self.state = MotionState.DETECTING
                        self.motion_start_time = now
                elif self.state == MotionState.DETECTING and now - (self.last_motion_time or now) > 3.0:
                    self.state = MotionState.IDLE
                    self.motion_start_time = None
            else:
                if person_now and (self.event_trigger != "person" or score > self._event_best_score):
                    if self.event_trigger != "person":
                        self.person_events += 1
                        logger.info("Event upgraded: person seen")
                    self.event_trigger = "person"
                    self._event_best_score = max(self._event_best_score, score)
                    frame_event = MotionEvent(
                        timestamp=now, motion_area=area, frame=frame, trigger="person",
                        person_score=score, person_boxes=[d.box for d in dets],
                        snapshot=self._snapshot(frame, dets),
                    )
                elif active_signal:
                    frame_event = MotionEvent(timestamp=now, motion_area=area, trigger=self.event_trigger)
                elif now - (self.last_motion_time or now) >= self.motion_cooldown:
                    logger.info(f"Event ended after {self.motion_cooldown:.0f}s without activity")
                    end_event = MotionEvent(timestamp=now, motion_area=0, trigger=self.event_trigger)
                    self.state = MotionState.IDLE
                    self.motion_start_time = None
                    self.last_motion_time = None
                    self.event_trigger = None
                    self._event_best_score = 0.0
                    self._person_hits.clear()
                    self._motion_window.clear()

        # Callbacks outside the lock
        if start_event and self.on_motion_start:
            self.on_motion_start(start_event)
        if frame_event:
            if self.on_motion_frame:
                self.on_motion_frame(frame_event)
            if frame_event.snapshot is not None and self.on_person:
                self.on_person(frame_event)
        if end_event and self.on_motion_end:
            self.on_motion_end(end_event)

    # ── Loop ───────────────────────────────────────────────────────────

    def _run_loop(self):
        logger.info(f"Starting detection on {self.stream_url} at {self.fps} fps")
        logger.info(f"Motion threshold {self.motion_threshold}, cooldown {self.motion_cooldown}s, "
                    f"motion fallback {self.motion_fallback}s, person score {self.person_score}")
        logger.info(f"ROI: x={self.roi_x_start}-{self.roi_x_end}%, y={self.roi_y_start}-{self.roi_y_end}%")
        self._load_person_detector()
        self._reader.start()
        last_reload = 0.0
        while self._running:
            if time.time() - last_reload > 1.0:
                self._reload_settings_from_file()
                last_reload = time.time()
            item = self._reader.get(timeout=5.0)
            if item is None:
                continue
            try:
                self._process(*item)
            except Exception as e:
                logger.exception(f"Error processing frame: {e}")
        self._reader.stop()
        logger.info("Motion detection stopped")

    def start(self):
        if self._running:
            logger.warning("Motion detector already running")
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="detector")
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=10)
            self._thread = None

    @property
    def is_motion_active(self) -> bool:
        with self._lock:
            return self.state == MotionState.ACTIVE

    @property
    def is_person_present(self) -> bool:
        return self._last_person_time > 0 and time.time() - self._last_person_time < 5.0

    def set_roi(self, x_start: int, x_end: int, y_start: int = None, y_end: int = None):
        with self._lock:
            self.roi_x_start = max(0, min(100, x_start))
            self.roi_x_end = max(0, min(100, x_end))
            if y_start is not None:
                self.roi_y_start = max(0, min(100, y_start))
            if y_end is not None:
                self.roi_y_end = max(0, min(100, y_end))
            self._bg = None  # ROI size changed
            logger.info(f"ROI updated: x={self.roi_x_start}%-{self.roi_x_end}%, y={self.roi_y_start}%-{self.roi_y_end}%")

    def set_threshold(self, threshold: int):
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
                return
            self._settings_mtime = mtime
            with open(self.settings_file, 'r') as f:
                content = f.read().strip()
            if not content:
                return
            settings = json.loads(content)

            roi = [self.roi_x_start, self.roi_x_end, self.roi_y_start, self.roi_y_end]
            new_roi = [
                max(0, min(100, int(settings.get(k, v))))
                for k, v in zip(('roi_x_start', 'roi_x_end', 'roi_y_start', 'roi_y_end'), roi)
            ]
            if new_roi != roi:
                self.set_roi(*new_roi)
            if 'motion_threshold' in settings and int(settings['motion_threshold']) != self.motion_threshold:
                self.set_threshold(int(settings['motion_threshold']))
            if 'person_score' in settings:
                self.person_score = max(0.2, min(0.95, float(settings['person_score'])))
            if 'motion_fallback' in settings:
                self.motion_fallback = max(0.0, float(settings['motion_fallback']))
        except Exception as e:
            logger.warning(f"Error reloading settings: {e}")

    def get_stats(self) -> dict:
        return {
            "frames_processed": self.frames_processed,
            "motion_events": self.motion_events,
            "person_events": self.person_events,
            "state": self.state.value,
            "is_motion_active": self.is_motion_active,
            "person_detected": self.is_person_present,
            "event_trigger": self.event_trigger,
            "last_person_score": round(self.last_person_score, 3),
            "last_motion_area": self.last_motion_area,
            "brightness": round(self.brightness, 1),
            "scene": self.scene,
            "light_changes": self.light_changes,
            "camera_moves": self.camera_moves,
            "person_detector": bool(self._person_detector),
            "person_runs": self.person_runs,
            "person_ms": round(self._person_ms),
            "stream_restarts": self._reader.restarts,
            "roi_x_start": self.roi_x_start,
            "roi_x_end": self.roi_x_end,
            "roi_y_start": self.roi_y_start,
            "roi_y_end": self.roi_y_end,
            "motion_threshold": self.motion_threshold,
            "person_score": self.person_score,
            "motion_fallback": self.motion_fallback,
        }
