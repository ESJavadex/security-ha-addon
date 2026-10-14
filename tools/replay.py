#!/usr/bin/env python3
"""
Replay recorded videos through the detector and report the events it would raise.

    python3 tools/replay.py --model /opt/seccam/models/yolox_s.onnx video1.mp4 [video2.mp4 ...]

Prints one JSON line per video: events (trigger, start offset), max person score,
light changes. Used to tune the detector against real footage without waiting
for people to walk by.
"""

import argparse
import json
import os
import subprocess
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'security-camera'))

from motion_detector import MotionDetector, WORK_W, WORK_H  # noqa: E402


def frames(path, fps):
    cmd = ['ffmpeg', '-v', 'error', '-nostdin', '-i', path, '-an',
           '-vf', f'fps={fps},scale={WORK_W}:{WORK_H}', '-pix_fmt', 'bgr24', '-f', 'rawvideo', '-']
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    size = WORK_W * WORK_H * 3
    i = 0
    while True:
        buf = p.stdout.read(size)
        if len(buf) < size:
            break
        yield i / fps, np.frombuffer(buf, np.uint8).reshape(WORK_H, WORK_W, 3)
        i += 1
    p.wait()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('videos', nargs='+')
    ap.add_argument('--model', default=None)
    ap.add_argument('--fps', type=float, default=4.0)
    ap.add_argument('--threshold', type=int, default=5000)
    ap.add_argument('--fallback', type=float, default=8.0)
    ap.add_argument('--person-score', type=float, default=0.5)
    ap.add_argument('--snapshots', default=None, help='dir to save person snapshots')
    a = ap.parse_args()

    for path in a.videos:
        events = []
        det = MotionDetector(
            stream_url='replay', motion_threshold=a.threshold, motion_cooldown=10,
            roi_x_start=0, roi_x_end=100, roi_y_start=5, roi_y_end=85,
            fps=a.fps, person_model=a.model, person_score=a.person_score,
            motion_fallback=a.fallback,
        )
        det._load_person_detector()
        # Replay timestamps start at a fixed epoch so cooldowns behave like live
        base = 1_000_000.0

        def on_start(ev, events=events, path=path):
            events.append({'trigger': ev.trigger, 't': round(ev.timestamp - base, 1),
                           'score': round(ev.person_score or 0, 2)})
            if a.snapshots and ev.snapshot is not None:
                import cv2
                os.makedirs(a.snapshots, exist_ok=True)
                cv2.imwrite(os.path.join(a.snapshots, os.path.basename(path) + '.jpg'), ev.snapshot)

        det.on_motion_start = on_start
        max_score = 0.0
        n = 0
        for t, f in frames(path, a.fps):
            det._process(base + t, f)
            max_score = max(max_score, det.last_person_score)
            n += 1
        print(json.dumps({
            'video': os.path.basename(path), 'frames': n, 'events': events,
            'max_person_score': round(max_score, 2), 'light_changes': det.light_changes,
            'person_runs': det.person_runs,
        }), flush=True)


if __name__ == '__main__':
    main()
