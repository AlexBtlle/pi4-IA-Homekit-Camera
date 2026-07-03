"""Tests for RtspPublisher — backoff behaviour and pipe draining (#33).

No hardware needed: subprocess and the clock are faked for the backoff logic,
and the drain tests use a real os.pipe().
"""
import os
import select
import time

from camera import rtsp_publisher
from camera.rtsp_publisher import RtspPublisher


class FakeProc:
    """Stands in for subprocess.Popen: exits immediately when waited on."""

    returncode = 1

    def wait(self):
        pass

    def terminate(self):
        pass


def make_publisher() -> RtspPublisher:
    return RtspPublisher(0, "rtsp://127.0.0.1:8554/camera")


def run_with_fake_time(pub, monkeypatch, run_durations):
    """
    Drive _run() through one spawn/exit cycle per entry in run_durations
    (seconds ffmpeg 'ran' before exiting), recording the drain delays used.
    """
    delays = []
    monkeypatch.setattr(pub, "_drain_pipe", lambda s: delays.append(s))

    # _run reads the clock twice per iteration: at spawn and after wait.
    ticks = []
    now = 0.0
    for ran in run_durations:
        ticks.append(now)          # started = monotonic()
        now += ran
        ticks.append(now)          # monotonic() - started
        now += 100.0               # arbitrary gap between incidents
    clock = iter(ticks)
    monkeypatch.setattr(rtsp_publisher.time, "monotonic", lambda: next(clock))

    spawns = {"n": 0}

    def fake_popen(*args, **kwargs):
        spawns["n"] += 1
        if spawns["n"] >= len(run_durations):
            # last cycle: stop after this proc exits
            pub._stop_event.set()
        return FakeProc()

    monkeypatch.setattr(rtsp_publisher.subprocess, "Popen", fake_popen)
    pub._run()
    return delays


# ----------------------------------------------------------------------
# Backoff
# ----------------------------------------------------------------------

def test_backoff_doubles_on_quick_exits(monkeypatch):
    pub = make_publisher()
    # 4 quick crashes (1 s each); the 4th stops the loop before its drain
    delays = run_with_fake_time(pub, monkeypatch, [1, 1, 1, 1])
    assert delays == [1, 2, 4]


def test_backoff_caps_at_max(monkeypatch):
    pub = make_publisher()
    delays = run_with_fake_time(pub, monkeypatch, [1] * 8)
    assert delays[-1] == rtsp_publisher._MAX_BACKOFF
    assert max(delays) == rtsp_publisher._MAX_BACKOFF


def test_backoff_resets_after_healthy_run(monkeypatch):
    pub = make_publisher()
    # crash fast twice (delay reaches 4), then run 300 s (healthy) → the next
    # incident restarts from 1 s (not compounding), then doubles again
    delays = run_with_fake_time(pub, monkeypatch, [1, 1, 300, 1, 1])
    assert delays == [1, 2, 1, 2]


def test_short_run_does_not_reset(monkeypatch):
    pub = make_publisher()
    # 59 s is under the healthy threshold: backoff keeps growing
    delays = run_with_fake_time(pub, monkeypatch, [1, 59, 1, 1])
    assert delays == [1, 2, 4]


def test_ffmpeg_has_io_timeout(monkeypatch):
    # A hung mediamtx must make ffmpeg exit (zombie mode, #34) so the drain +
    # backoff loop can take over — the args must carry an I/O timeout.
    pub = make_publisher()
    monkeypatch.setattr(pub, "_drain_pipe", lambda s: None)
    captured = {}

    def fake_popen(args, **kwargs):
        captured["args"] = args
        pub._stop_event.set()
        return FakeProc()

    monkeypatch.setattr(rtsp_publisher.subprocess, "Popen", fake_popen)
    pub._run()
    args = captured["args"]
    assert "-rw_timeout" in args
    timeout_us = int(args[args.index("-rw_timeout") + 1])
    # must expire comfortably under the 10 s frame watchdog
    assert 0 < timeout_us <= 8_000_000


# ----------------------------------------------------------------------
# Pipe draining
# ----------------------------------------------------------------------

def test_drain_empties_the_pipe_and_honours_deadline():
    r, w = os.pipe()
    try:
        pub = RtspPublisher(r, "rtsp://x")
        os.write(w, b"x" * 32768)
        t0 = time.monotonic()
        pub._drain_pipe(0.3)
        assert time.monotonic() - t0 < 2.0  # returned around the deadline
        ready, _, _ = select.select([r], [], [], 0)
        assert not ready  # everything was consumed
    finally:
        os.close(r)
        os.close(w)


def test_drain_returns_promptly_on_stop():
    r, w = os.pipe()
    try:
        pub = RtspPublisher(r, "rtsp://x")
        pub._stop_event.set()
        t0 = time.monotonic()
        pub._drain_pipe(10.0)  # would be far too long without the stop check
        assert time.monotonic() - t0 < 0.5
    finally:
        os.close(r)
        os.close(w)
