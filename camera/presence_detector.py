import json
import logging
import threading
import time
import urllib.request

import cv2

from .camera_manager import CameraManager

logger = logging.getLogger(__name__)


class PresenceDetector:
    """
    Background daemon thread: MOG2 motion detection.

    On trigger: HTTP POST to the local HomeKit app's motion endpoint, which
    sets the HomeKit MotionSensor to true and arms HKSV recording. The app
    resets the sensor automatically after its motion timeout.
    Person/animal/vehicle classification is handled by HomeKit Secure Video
    on the home hub (Apple TV / HomePod).
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
        self._cooldown = float(cfg.get("cooldown", 30))
        analysis_fps = float(cfg.get("analysis_fps", 10))
        self._frame_interval = 1.0 / analysis_fps if analysis_fps > 0 else 0.0
        self._last_analysis = 0.0

        hk_cfg = config.get("homekit", {})
        motion_port = int(hk_cfg.get("motion_port", 8989))
        self._webhook_url = f"http://localhost:{motion_port}/motion"

        self._stop_event = threading.Event()
        self._last_trigger = 0.0

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
        warmup = self.WARMUP_FRAMES
        logger.info("Detection loop running (MOG2 motion-only — HKSV classifies on the hub)")

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

            fg_mask = mog2.apply(frame)
            fg_mask = cv2.morphologyEx(fg_mask, cv2.MORPH_OPEN, kernel)
            contours, _ = cv2.findContours(
                fg_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
            )
            max_area = max((cv2.contourArea(c) for c in contours), default=0)
            if max_area < self._min_area:
                continue

            now = time.monotonic()
            if now - self._last_trigger < self._cooldown:
                continue

            self._last_trigger = now
            logger.info("Motion detected — sending webhook (area=%d)", int(max_area))
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
