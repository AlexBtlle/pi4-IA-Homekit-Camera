import logging
import os
import threading
import time
import urllib.parse
import urllib.request

import cv2
import numpy as np

from .camera_manager import CameraManager

logger = logging.getLogger(__name__)

PERSON_CLASS_ID = 0  # COCO label 0 = person in MobileNet SSD v1


class PresenceDetector:
    """
    Background daemon thread: two-stage presence detection.
      Stage 1 — MOG2 background subtraction  (~2 ms, always on)
      Stage 2 — TFLite MobileNet SSD person detection (~80 ms, only on motion)

    On detection: HTTP GET to homebridge-camera-ffmpeg's porthttp endpoint,
    which sets the HomeKit MotionSensor to true and triggers HKSV recording.
    homebridge resets the sensor automatically after motionTimeout seconds.
    """

    WARMUP_FRAMES = 60

    def __init__(self, camera_manager: CameraManager, config: dict):
        self._camera_manager = camera_manager

        cfg = config.get("detection", {})
        self._enabled = bool(cfg.get("enabled", True))
        self._mog2_history = int(cfg.get("mog2_history", 500))
        self._mog2_threshold = int(cfg.get("mog2_var_threshold", 40))
        self._mog2_shadows = bool(cfg.get("mog2_detect_shadows", False))
        self._min_area = int(cfg.get("min_motion_area", 1500))
        self._person_threshold = float(cfg.get("person_confidence", 0.55))
        self._cooldown = float(cfg.get("cooldown", 60))

        hb_cfg = config.get("homebridge", {})
        porthttp = int(hb_cfg.get("porthttp", 8889))
        camera_name = hb_cfg.get("camera_name", "Pi Camera")
        # homebridge-camera-ffmpeg HTTP motion trigger:
        # GET http://localhost:<porthttp>/<camera-name>?motion=true
        self._webhook_url = (
            f"http://localhost:{porthttp}/"
            f"{urllib.parse.quote(camera_name)}?motion=true"
        )

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
            logger.info("PresenceDetector started (webhook → %s)", self._webhook_url)
        else:
            logger.info("PresenceDetector disabled in config")

    def stop(self) -> None:
        self._stop_event.set()

    # ------------------------------------------------------------------
    # Detection loop
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

            if warmup > 0:
                mog2.apply(frame)
                warmup -= 1
                continue

            # Stage 1: background subtraction
            fg_mask = mog2.apply(frame)
            fg_mask = cv2.morphologyEx(fg_mask, cv2.MORPH_OPEN, kernel)
            contours, _ = cv2.findContours(
                fg_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
            )
            if not any(cv2.contourArea(c) >= self._min_area for c in contours):
                continue

            # Cooldown gate
            now = time.monotonic()
            if now - self._last_trigger < self._cooldown:
                continue

            # Stage 2: TFLite person detection
            person_detected = self._run_tflite(frame) if model_ok else True

            if person_detected:
                self._last_trigger = now
                logger.info("Person detected — sending motion webhook")
                threading.Thread(
                    target=self._send_webhook, daemon=True
                ).start()

    # ------------------------------------------------------------------
    # Webhook
    # ------------------------------------------------------------------

    def _send_webhook(self) -> None:
        try:
            urllib.request.urlopen(self._webhook_url, timeout=2)
            logger.debug("Motion webhook OK")
        except Exception:
            logger.warning("Motion webhook failed", exc_info=True)

    # ------------------------------------------------------------------
    # TFLite
    # ------------------------------------------------------------------

    def _load_model(self) -> bool:
        try:
            from tflite_runtime.interpreter import Interpreter
        except ImportError:
            logger.warning("tflite_runtime not installed — stage 2 disabled")
            return False

        model_path = self._find_model()
        if not model_path:
            logger.warning("detect.tflite not found — stage 2 disabled")
            return False

        try:
            self._interpreter = Interpreter(model_path=model_path)
            self._interpreter.allocate_tensors()
            self._input_details = self._interpreter.get_input_details()
            self._output_details = self._interpreter.get_output_details()
            self._input_h = self._input_details[0]["shape"][1]
            self._input_w = self._input_details[0]["shape"][2]
            logger.info("TFLite model loaded: %s (%dx%d)", model_path,
                        self._input_w, self._input_h)
            return True
        except Exception:
            logger.exception("Failed to load TFLite model")
            return False

    def _find_model(self) -> str | None:
        candidates = [
            os.path.join(os.path.dirname(__file__), "..", "models", "detect.tflite"),
            "/opt/pi4cam/models/detect.tflite",
        ]
        for path in candidates:
            if os.path.isfile(path):
                return os.path.abspath(path)
        return None

    def _run_tflite(self, y_frame: np.ndarray) -> bool:
        resized = cv2.resize(y_frame, (self._input_w, self._input_h))
        rgb = np.stack([resized, resized, resized], axis=-1)[np.newaxis]

        self._interpreter.set_tensor(self._input_details[0]["index"], rgb)
        self._interpreter.invoke()

        classes = self._interpreter.get_tensor(self._output_details[1]["index"])[0]
        scores  = self._interpreter.get_tensor(self._output_details[2]["index"])[0]
        num     = int(self._interpreter.get_tensor(self._output_details[3]["index"])[0])

        for i in range(num):
            if int(classes[i]) == PERSON_CLASS_ID and float(scores[i]) >= self._person_threshold:
                logger.debug("Person score=%.2f", scores[i])
                return True
        return False
