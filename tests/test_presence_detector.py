"""Tests for the motion episode state machine (#40) — no hardware.

Guarantee under test: one movement EPISODE = one sensor rising edge = one iOS
notification and one uncut HKSV clip. An episode spans motions separated by
gaps shorter than `cooldown` (a wandering cat pausing 15 s stays in the same
episode), the webhook is refreshed through the pauses so the Node-side sensor
never resets mid-episode, and only a full `cooldown` of quiet ends it.
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


def test_episode_ends_after_cooldown_then_new_motion_notifies():
    det = make_detector()
    assert drive(det, [(100.0, True)]) == [100.0]
    # quiet spell: refreshes keep the sensor alive while the gap is < cooldown
    sent = drive(det, [(100.0 + t, False) for t in range(1, 31)])
    assert sent == [105.0, 110.0, 115.0, 120.0, 125.0]
    # a full cooldown (30 s) of quiet ends the episode
    assert det._episode_active is False
    # motion after that is a genuinely new event → immediate rising edge
    assert drive(det, [(140.0, True)]) == [140.0]


def test_pause_shorter_than_cooldown_stays_in_the_episode():
    det = make_detector()
    # motion, 20 s pause (>> motion_timeout 10 s, < cooldown 30 s), motion.
    # The old machine ended the episode at 10 s idle then DROPPED the resumed
    # motion (cooling down): second notification later, and worse, a coverage
    # hole. Now: same episode, sensor held through the pause.
    sent = drive(
        det,
        [(100.0, True)]
        + [(100.0 + t, False) for t in range(1, 21)]
        + [(120.5, True)],
    )
    assert sent[0] == 100.0
    assert det._episode_active is True
    # the Node sensor (motion_timeout 10 s) never saw a gap → no reset, no
    # second rising edge, no extra notification
    gaps = [b - a for a, b in zip(sent, sent[1:])]
    assert gaps and all(g < 10 for g in gaps)


def test_wandering_cat_is_one_episode():
    det = make_detector()
    # five 3 s bursts of motion separated by 15 s pauses — the screenshot
    # scenario (several "animal detected" notifications for one wander)
    events, t = [], 100.0
    for _ in range(5):
        events += [(t + i, True) for i in range(3)]
        t += 3
        events += [(t + i, False) for i in range(1, 15)]
        t += 15
    sent = drive(det, events)
    assert det._episode_active is True          # still one live episode
    gaps = [b - a for a, b in zip(sent, sent[1:])]
    assert all(g < 10 for g in gaps)            # sensor never reset → 1 edge


def test_refresh_interval_respected_inside_episode():
    det = make_detector()
    sent = drive(det, [(100.0, True), (101.0, True), (104.9, True), (105.0, True)])
    assert sent == [100.0, 105.0]  # no spam between refreshes
