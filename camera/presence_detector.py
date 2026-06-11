import json
import logging
import os
import threading
import time
import urllib.request

import cv2
import numpy as np

from .camera_manager import CameraManager

logger = logging.getLogger(__name__)

# MobileNet-SSD (Caffe, trained on Pascal VOC) — class index 15 = person.
# Run via OpenCV's DNN module: no fragile ML runtime to install, only opencv.
PERSON_CLASS_ID = 15
DNN_INPUT_SIZE = 300
DNN_SCALE = 0.007843     # 1 / 127.5
DNN_MEAN = 127.5


class PresenceDetector:
    """
    Background daemon thread: motion-based presence detection.
      Stage 1 — MOG2 background subtraction  (~2 ms, always on)
      Stage 2 — OpenCV DNN MobileNet-SSD person filter (optional, opt-in via
                require_person; ~80 ms, only runs on motion)

    On trigger: HTTP POST to the local HomeKit app's motion endpoint, which
    sets the HomeKit MotionSensor to true and arms HKSV recording. The app
    resets the sensor automatically after its motion timeout.
    By default person/animal/vehicle classification is left to HomeKit Secure
    Video on the home hub (Apple TV / HomePod).
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
        self._cooldown = float(cfg.get("cooldown", 30))
        # Local person filter is optional; by default we trigger on motion and
        # let HomeKit Secure Video classify person/animal/vehicle on the hub.
        self._require_person = bool(cfg.get("require_person", False))
        self._debug = bool(cfg.get("debug", False))
        self._last_diag = 0.0
        analysis_fps = float(cfg.get("analysis_fps", 10))
        self._frame_interval = 1.0 / analysis_fps if analysis_fps > 0 else 0.0
        self._last_analysis = 0.0

        hk_cfg = config.get("homekit", {})
        motion_port = int(hk_cfg.get("motion_port", 8989))
        # Our HAP-NodeJS app exposes a tiny motion endpoint on localhost:
        #   POST http://localhost:<motion_port>/motion
        # → triggers the HomeKit MotionSensor and arms HKSV recording.
        self._webhook_url = f"http://localhost:{motion_port}/motion"

        self._stop_event = threading.Event()
        self._last_trigger = 0.0
        self._net = None  # cv2.dnn network (loaded in the worker thread)

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
        # Only load the DNN if a local person filter was explicitly requested.
        model_ok = self._load_model() if self._require_person else False
        warmup = self.WARMUP_FRAMES
        logger.info(
            "Detection loop running (mode: %s)",
            "motion + local person filter" if model_ok
            else "motion-only — HKSV classifies on the hub",
        )

        while not self._stop_event.is_set():
            frame = self._camera_manager.get_lores_frame(timeout=1.0)
            if frame is None:
                continue

            if warmup > 0:
                mog2.apply(frame)
                warmup -= 1
                continue

            now = time.monotonic()
            if self._frame_interval > 0 and (now - self._last_analysis) < self._frame_interval:
                continue
            self._last_analysis = now

            # Stage 1: background subtraction
            fg_mask = mog2.apply(frame)
            fg_mask = cv2.morphologyEx(fg_mask, cv2.MORPH_OPEN, kernel)
            contours, _ = cv2.findContours(
                fg_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
            )
            max_area = max((cv2.contourArea(c) for c in contours), default=0)
            if max_area < self._min_area:
                continue

            # Cooldown gate
            now = time.monotonic()
            if now - self._last_trigger < self._cooldown:
                continue

            # Stage 2 (optional): local person filter before triggering.
            if model_ok:
                person, best_conf = self._run_dnn(frame)
                if self._debug and now - self._last_diag >= 2.0:
                    self._last_diag = now
                    logger.info(
                        "motion area=%d  person_conf=%.2f (thr=%.2f) → %s",
                        int(max_area), best_conf, self._person_threshold,
                        "PERSON" if person else "no person",
                    )
                if not person:
                    continue

            self._last_trigger = now
            logger.info(
                "Motion%s detected — sending webhook (area=%d)",
                " + person" if model_ok else "", int(max_area),
            )
            threading.Thread(target=self._send_webhook, daemon=True).start()

    # ------------------------------------------------------------------
    # Webhook
    # ------------------------------------------------------------------

    def _send_webhook(self) -> None:
        try:
            payload = json.dumps({"source": "pi4cam"}).encode()
            req = urllib.request.Request(
                self._webhook_url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=2)
            logger.debug("Motion webhook OK")
        except Exception:
            logger.warning("Motion webhook failed", exc_info=True)

    # ------------------------------------------------------------------
    # OpenCV DNN — MobileNet-SSD person detection
    # ------------------------------------------------------------------

    def _load_model(self) -> bool:
        prototxt, caffemodel = self._find_model()
        if not (prototxt and caffemodel):
            logger.warning("MobileNet-SSD model files not found — stage 2 disabled")
            return False

        try:
            self._net = cv2.dnn.readNetFromCaffe(prototxt, caffemodel)
            logger.info("MobileNet-SSD loaded via OpenCV DNN: %s", caffemodel)
            return True
        except Exception:
            logger.exception("Failed to load MobileNet-SSD model")
            return False

    def _find_model(self) -> tuple[str | None, str | None]:
        dirs = [
            os.path.join(os.path.dirname(__file__), "..", "models"),
            "/opt/pi4cam/models",
        ]
        for d in dirs:
            proto = os.path.join(d, "MobileNetSSD_deploy.prototxt")
            model = os.path.join(d, "MobileNetSSD_deploy.caffemodel")
            if os.path.isfile(proto) and os.path.isfile(model):
                return os.path.abspath(proto), os.path.abspath(model)
        return None, None

    def _run_dnn(self, y_frame: np.ndarray) -> tuple[bool, float]:
        """Returns (person_above_threshold, best_person_confidence)."""
        # The model expects 3-channel BGR input; our lores frame is the Y
        # (luma) plane, so replicate it across the three channels.
        bgr = cv2.cvtColor(y_frame, cv2.COLOR_GRAY2BGR)
        blob = cv2.dnn.blobFromImage(
            bgr, DNN_SCALE, (DNN_INPUT_SIZE, DNN_INPUT_SIZE), DNN_MEAN
        )
        self._net.setInput(blob)
        detections = self._net.forward()  # shape (1, 1, N, 7)

        best_conf = 0.0
        for i in range(detections.shape[2]):
            class_id = int(detections[0, 0, i, 1])
            confidence = float(detections[0, 0, i, 2])
            if class_id == PERSON_CLASS_ID:
                best_conf = max(best_conf, confidence)
        return best_conf >= self._person_threshold, best_conf
