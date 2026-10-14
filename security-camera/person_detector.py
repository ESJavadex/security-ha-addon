#!/usr/bin/env python3
"""
Person detector based on YOLOX (Apache-2.0) run with OpenCV DNN.

Model: object_detection_yolox_2022nov.onnx from opencv_zoo (YOLOX-s, COCO, 640x640).
Only the COCO "person" class (0) is reported.
"""

import logging
from dataclasses import dataclass
from typing import List, Optional, Tuple

import cv2
import numpy as np

logger = logging.getLogger(__name__)

PERSON_CLASS = 0


@dataclass
class Detection:
    score: float
    box: Tuple[int, int, int, int]  # x1, y1, x2, y2 in frame pixels

    @property
    def area(self) -> int:
        x1, y1, x2, y2 = self.box
        return max(0, x2 - x1) * max(0, y2 - y1)


class PersonDetector:
    def __init__(
        self,
        model_path: str,
        input_size: int = 640,
        score_threshold: float = 0.45,
        nms_threshold: float = 0.45,
        threads: int = 1,
    ):
        self.model_path = model_path
        self.input_size = input_size
        self.score_threshold = score_threshold
        self.nms_threshold = nms_threshold
        cv2.setNumThreads(max(1, threads))
        self.net = cv2.dnn.readNet(model_path)
        self.net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
        self.net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
        self._grids, self._strides = self._make_grids(input_size)
        logger.info(f"Person detector loaded: {model_path} (input {input_size})")

    @staticmethod
    def _make_grids(size: int):
        grids, strides = [], []
        for stride in (8, 16, 32):
            n = size // stride
            yv, xv = np.meshgrid(np.arange(n), np.arange(n), indexing='ij')
            grids.append(np.stack((xv, yv), 2).reshape(-1, 2))
            strides.append(np.full((n * n, 1), stride))
        return np.concatenate(grids, 0).astype(np.float32), np.concatenate(strides, 0).astype(np.float32)

    def _letterbox(self, frame: np.ndarray) -> Tuple[np.ndarray, float]:
        h, w = frame.shape[:2]
        ratio = min(self.input_size / h, self.input_size / w)
        resized = cv2.resize(frame, (int(w * ratio), int(h * ratio)), interpolation=cv2.INTER_LINEAR)
        padded = np.full((self.input_size, self.input_size, 3), 114, dtype=np.uint8)
        padded[:resized.shape[0], :resized.shape[1]] = resized
        return padded, ratio

    def detect(self, frame: np.ndarray, score_threshold: Optional[float] = None) -> List[Detection]:
        """Return person detections for a BGR frame."""
        thr = self.score_threshold if score_threshold is None else score_threshold
        if frame.ndim == 2:
            frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
        img, ratio = self._letterbox(frame)
        blob = np.transpose(img, (2, 0, 1))[np.newaxis].astype(np.float32)
        self.net.setInput(blob)
        out = self.net.forward()[0]  # (N, 85)

        scores = out[:, 4] * out[:, 5 + PERSON_CLASS]
        keep = scores >= thr
        if not np.any(keep):
            return []
        out = out[keep]
        scores = scores[keep]
        grids = self._grids[keep]
        strides = self._strides[keep]

        cxcy = (out[:, :2] + grids) * strides
        wh = np.exp(out[:, 2:4]) * strides
        boxes = np.concatenate([cxcy - wh / 2, wh], axis=1) / ratio  # x, y, w, h

        idx = cv2.dnn.NMSBoxes(boxes.tolist(), scores.tolist(), thr, self.nms_threshold)
        h, w = frame.shape[:2]
        dets = []
        for i in np.array(idx).flatten():
            x, y, bw, bh = boxes[i]
            x1, y1 = int(max(0, x)), int(max(0, y))
            x2, y2 = int(min(w, x + bw)), int(min(h, y + bh))
            if x2 > x1 and y2 > y1:
                dets.append(Detection(float(scores[i]), (x1, y1, x2, y2)))
        dets.sort(key=lambda d: d.score, reverse=True)
        return dets
