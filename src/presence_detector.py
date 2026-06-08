import asyncio
import logging
import os
import threading
import time

import cv2
import numpy as np

from .camera_manager import CameraManager
from .motion_sensor import MotionSensor

logger = logging.getLogger(__name__)

PERSON_CLASS_ID = 0  # COCO label 0 = person in MobileNet SSD v1


class PresenceDetector:
    """
    Background daemon thread performing two-stage presence detection:
      Stage 1: OpenCV MOG2 background subtraction (fast, no ML)
      Stage 2: TFLite MobileNet SSD v1 person detection (only on motion events)

    Results are forwarded to HAP-python via driver.add_job() — the only
    thread-safe way to update HomeKit characteristics from outside asyncio.
    """

    # Skip first N frames so AGC/AWB and MOG2 background model can stabilise
    WARMUP_FRAMES = 60

    def __init__(self, camera_manager: CameraManager, motion_sensor: MotionSensor,
                 driver, config: dict):
        self._camera_manager = camera_manager
        self._motion_sensor = motion_sensor
        self._driver = driver

        cfg = config.get("detection", {})
        self._enabled = bool(cfg.get("enabled", True))
        self._mog2_history = int(cfg.get("mog2_history", 500))
        self._mog2_threshold = int(cfg.get("mog2_var_threshold", 40))
        self._mog2_shadows = bool(cfg.get("mog2_detect_shadows", False))
        self._min_area = int(cfg.get("min_motion_area", 1500))
        self._person_threshold = float(cfg.get("person_confidence", 0.55))
        self._cooldown = float(cfg.get("cooldown", 60))
        self._reset_delay = float(cfg.get("reset_delay", 5))

        self._stop_event = threading.Event()
        self._last_trigger = 0.0
        self._interpreter = None
        self._input_details = None
        self._output_details = None
        self._input_w = 300
        self._input_h = 300

        self._thread = threading.Thread(
            target=self._run, daemon=True, name="presence-detector"
        )

    def start(self) -> None:
        if self._enabled:
            self._thread.start()
            logger.info("PresenceDetector started")
        else:
            logger.info("PresenceDetector disabled in config")

    def stop(self) -> None:
        self._stop_event.set()

    # ------------------------------------------------------------------
    # Main detection loop
    # ------------------------------------------------------------------

    def _run(self) -> None:
        mog2 = cv2.createBackgroundSubtractorMOG2(
            history=self._mog2_history,
            varThreshold=self._mog2_threshold,
            detectShadows=self._mog2_shadows,
        )
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        model_ok = self._load_model()
        warmup = self.WARMUP_FRAMES
        logger.info("Detection loop running (TFLite: %s)", model_ok)

        while not self._stop_event.is_set():
            frame = self._camera_manager.get_lores_frame(timeout=1.0)
            if frame is None:
                continue

            # Feed MOG2 during warm-up to build a valid background model
            if warmup > 0:
                mog2.apply(frame)
                warmup -= 1
                continue

            # --- Stage 1: background subtraction ---
            fg_mask = mog2.apply(frame)
            fg_mask = cv2.morphologyEx(fg_mask, cv2.MORPH_OPEN, kernel)
            contours, _ = cv2.findContours(
                fg_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
            )
            if not any(cv2.contourArea(c) >= self._min_area for c in contours):
                continue

            # --- Cooldown gate ---
            now = time.monotonic()
            if now - self._last_trigger < self._cooldown:
                continue

            # --- Stage 2: TFLite person detection ---
            person_detected = self._run_tflite(frame) if model_ok else True

            if person_detected:
                self._last_trigger = now
                logger.info("Person detected — triggering MotionSensor")
                self._driver.add_job(self._trigger_motion)

    # ------------------------------------------------------------------
    # TFLite inference
    # ------------------------------------------------------------------

    def _load_model(self) -> bool:
        try:
            from tflite_runtime.interpreter import Interpreter
        except ImportError:
            logger.warning("tflite_runtime not installed — stage 2 detection disabled")
            return False

        model_path = self._find_model()
        if not model_path:
            logger.warning("detect.tflite not found — stage 2 detection disabled")
            return False

        try:
            self._interpreter = Interpreter(model_path=model_path)
            self._interpreter.allocate_tensors()
            self._input_details = self._interpreter.get_input_details()
            self._output_details = self._interpreter.get_output_details()
            self._input_h = self._input_details[0]["shape"][1]
            self._input_w = self._input_details[0]["shape"][2]
            logger.info("TFLite model loaded: %s (%dx%d input)",
                        model_path, self._input_w, self._input_h)
            return True
        except Exception:
            logger.exception("Failed to load TFLite model")
            return False

    def _find_model(self) -> str | None:
        candidates = [
            os.path.join(os.path.dirname(__file__), "..", "models", "detect.tflite"),
            "/opt/pi4tohomekit/models/detect.tflite",
        ]
        for path in candidates:
            if os.path.isfile(path):
                return os.path.abspath(path)
        return None

    def _run_tflite(self, y_frame: np.ndarray) -> bool:
        """
        Run MobileNet SSD v1 quant inference on the Y (luma) plane.
        Input is greyscale; stacked to RGB because the model was trained on RGB.
        Returns True if any detection has class=person and score >= threshold.
        """
        resized = cv2.resize(y_frame, (self._input_w, self._input_h))
        # Stack Y channel three times to produce pseudo-RGB (1, h, w, 3)
        rgb = np.stack([resized, resized, resized], axis=-1)[np.newaxis]

        self._interpreter.set_tensor(self._input_details[0]["index"], rgb)
        self._interpreter.invoke()

        # MobileNet SSD v1 outputs: [boxes, classes, scores, num_detections]
        classes = self._interpreter.get_tensor(self._output_details[1]["index"])[0]
        scores = self._interpreter.get_tensor(self._output_details[2]["index"])[0]
        num = int(self._interpreter.get_tensor(self._output_details[3]["index"])[0])

        for i in range(num):
            if int(classes[i]) == PERSON_CLASS_ID and float(scores[i]) >= self._person_threshold:
                logger.debug("Person score=%.2f", scores[i])
                return True
        return False

    # ------------------------------------------------------------------
    # HomeKit update (runs in asyncio event loop via driver.add_job)
    # ------------------------------------------------------------------

    def _trigger_motion(self) -> None:
        """Scheduled by driver.add_job() — runs in asyncio context."""
        self._motion_sensor.set_motion(True)
        asyncio.ensure_future(self._reset_after_delay())

    async def _reset_after_delay(self) -> None:
        await asyncio.sleep(self._reset_delay)
        self._motion_sensor.set_motion(False)
