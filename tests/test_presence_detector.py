"""Tests for the motion episode state machine (#40) — no hardware.

Guarantee under test: a continuous movement keeps the HomeKit sensor active
for its WHOLE duration (webhook refreshed before motion_timeout expires), and
cooldown only separates episodes — it never truncates one.
"""
from unittest.mock import MagicMock

from camera.presence_detector import PresenceDetector

# defaults: cooldown=30, motion_timeout=10 → refresh=5, episode_idle=10
CONFIG = {"detection": {"cooldown": 30}, "homekit": {"motion_timeout": 10}}


def make_detector() -> PresenceDetector:
    return PresenceDetector(MagicMock(), CONFIG)


def drive(det, events):
    """Feed (time, motion) pairs; return the times a webhook was requested."""
    sent = []
    for now, motion in events:
        if det._process_motion(motion, now):
            sent.append(now)
    return sent


def test_first_motion_triggers_immediately():
    det = make_detector()
    assert drive(det, [(100.0, True)]) == [100.0]


def test_no_motion_never_sends():
    det = make_detector()
    assert drive(det, [(t, False) for t in range(100, 160)]) == []


def test_continuous_motion_keeps_sensor_alive():
    det = make_detector()
    # motion every second for 42 s
    sent = drive(det, [(100.0 + t, True) for t in range(43)])
    assert sent[0] == 100.0
    # refreshed regularly: every gap stays under motion_timeout (10 s), so
    # the Node-side sensor never resets mid-movement
    gaps = [b - a for a, b in zip(sent, sent[1:])]
    assert gaps and all(g <= 10 for g in gaps)
    # and the refreshes went on beyond the old 30 s cooldown horizon
    assert sent[-1] >= 140.0


def test_cooldown_does_not_truncate_an_episode():
    det = make_detector()
    # 60 s of continuous motion — well past cooldown (30 s)
    sent = drive(det, [(100.0 + t, True) for t in range(61)])
    assert any(t > 135.0 for t in sent)


def test_episode_ends_after_idle_and_cooldown_separates_episodes():
    det = make_detector()
    sent = drive(det, [(100.0, True)])
    assert sent == [100.0]
    # silence: episode expires after 10 s idle
    assert drive(det, [(111.0, False)]) == []
    assert det._episode_active is False
    # new motion 15 s after the episode's last movement → still cooling down
    assert drive(det, [(115.0, True)]) == []
    # 31 s after the last movement (episode ended at t=100) → new episode
    assert drive(det, [(131.0, True)]) == [131.0]


def test_brief_pause_within_episode_does_not_end_it():
    det = make_detector()
    drive(det, [(100.0, True)])
    # 8 s pause (below the 10 s idle), then motion resumes: same episode,
    # refresh fires because 8 s ≥ refresh interval (5 s)
    assert drive(det, [(104.0, False)]) == []
    assert drive(det, [(108.0, True)]) == [108.0]
    assert det._episode_active is True


def test_refresh_interval_respected_inside_episode():
    det = make_detector()
    sent = drive(det, [(100.0, True), (101.0, True), (104.9, True), (105.0, True)])
    assert sent == [100.0, 105.0]  # no spam between refreshes
