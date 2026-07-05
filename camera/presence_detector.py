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

        hk_cfg = config.get("homekit", {})
        motion_port = int(hk_cfg.get("motion_port", 8989))
        self._webhook_url = f"http://localhost:{motion_port}/motion"

        # Episode tracking (#40): a movement episode spans motions separated
        # by gaps shorter than `cooldown` — only that long a quiet spell ends
        # it. While the episode lasts, THROUGH THE PAUSES TOO, the webhook is
        # re-posted before the Node-side sensor reset (motion_timeout) fires.
        # iOS notifies on the inactive→active edge, so the sensor rising
        # exactly once per episode means one notification and one uncut HKSV
        # clip per movement, however stop-start it is (a wandering cat pauses
        # 15 s all the time — that used to fire a notification per resume).
        motion_timeout = float(hk_cfg.get("motion_timeout", 10))
        self._refresh_interval = max(1.0, motion_timeout / 2)
        self._episode_active = False
        self._episode_last_motion = 0.0
        self._last_webhook = 0.0

        self._stop_event = threading.Event()

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
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))

        # Warmup: feed MOG2 at full frame rate so the background model stabilises
        # before we start triggering detections.
        warmup = self.WARMUP_FRAMES
        logger.info("Detection loop starting — warmup %d frames", warmup)
        while not self._stop_event.is_set() and warmup > 0:
            frame = self._camera_manager.get_lores_frame(timeout=1.0)
            if frame is not None:
                mog2.apply(frame)
                warmup -= 1

        logger.info("MOG2 warmup done — detection active")

        while not self._stop_event.is_set():
            frame = self._camera_manager.get_lores_frame(timeout=1.0)
            now = time.monotonic()
            if frame is None:
                # time still passes: let an ongoing episode expire
                if self._process_motion(False, now):
                    self._send_webhook()
                continue

            fg_mask = mog2.apply(frame)
            fg_mask = cv2.morphologyEx(fg_mask, cv2.MORPH_OPEN, kernel)
            contours, _ = cv2.findContours(
                fg_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
            )
            max_area = max((cv2.contourArea(c) for c in contours), default=0)
            if self._process_motion(max_area >= self._min_area, now, int(max_area)):
                self._send_webhook()

    def _process_motion(self, motion: bool, now: float, area: int = 0) -> bool:
        """
        Episode state machine (#40). Returns True when a webhook must be sent.

        - An episode starts on motion and ends only after `cooldown` seconds
          WITHOUT motion: shorter pauses (a cat stopping to sniff) stay inside
          the same episode instead of splitting it.
        - While the episode is active — through the pauses too — the webhook
          is refreshed every _refresh_interval (< motion_timeout) so the
          HomeKit sensor never resets mid-episode. One rising edge per
          episode: one iOS notification, one uncut HKSV clip.
        - After the episode ends, the next motion is by definition ≥ cooldown
          away from the last one: a genuinely new event, notified immediately.
        """
        if motion:
            self._episode_last_motion = now
            if not self._episode_active:
                self._episode_active = True
                self._last_webhook = now
                logger.info("Motion episode started (area=%d)", area)
                return True
        if not self._episode_active:
            return False
        if now - self._episode_last_motion >= self._cooldown:
            self._episode_active = False
            logger.info("Motion episode ended")
            return False
        if now - self._last_webhook >= self._refresh_interval:
            self._last_webhook = now
            logger.debug("Motion episode continues — refreshing sensor (area=%d)", area)
            return True
        return False

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
            urllib.request.urlopen(req, timeout=5)
            logger.debug("Motion webhook OK")
        except Exception:
            logger.warning("Motion webhook failed", exc_info=True)
