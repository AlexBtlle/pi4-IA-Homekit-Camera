"""Tests for the localhost control endpoint and the live bitrate mechanism."""
import json
import urllib.error
import urllib.request

import pytest

from camera.camera_manager import CameraManager
from camera.control_server import ControlServer


# ----------------------------------------------------------------------
# ControlServer — HTTP surface
# ----------------------------------------------------------------------

@pytest.fixture
def server():
    calls = []

    def set_bitrate(bps: int) -> int:
        calls.append(bps)
        return bps

    srv = ControlServer(0, set_bitrate)  # port 0: ephemeral
    srv.start()
    yield srv, calls
    srv.stop()


def post(port: int, path: str, body: bytes):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    return urllib.request.urlopen(req, timeout=2)


def test_bitrate_endpoint_converts_and_replies(server):
    srv, calls = server
    resp = post(srv.port, "/bitrate", json.dumps({"kbps": 2000}).encode())
    assert calls == [2_000_000]
    assert json.loads(resp.read())["applied_bps"] == 2_000_000


def test_unknown_path_is_404(server):
    srv, calls = server
    with pytest.raises(urllib.error.HTTPError) as e:
        post(srv.port, "/reboot", b"{}")
    assert e.value.code == 404
    assert calls == []


def test_malformed_body_is_400(server):
    srv, calls = server
    with pytest.raises(urllib.error.HTTPError) as e:
        post(srv.port, "/bitrate", b"not json")
    assert e.value.code == 400
    assert calls == []


# ----------------------------------------------------------------------
# CameraManager.set_bitrate — clamping (mechanism side)
# ----------------------------------------------------------------------

def make_manager() -> CameraManager:
    cam = CameraManager({"camera": {"bitrate": 8_000_000}})
    cam._encoder = object()  # pretend the encoder is running
    return cam


def test_set_bitrate_applies_within_bounds(monkeypatch):
    cam = make_manager()
    applied = []
    monkeypatch.setattr(cam, "_apply_bitrate", lambda b: applied.append(b))
    assert cam.set_bitrate(2_000_000) == 2_000_000
    assert applied == [2_000_000]


def test_set_bitrate_clamps_to_floor_and_ceiling(monkeypatch):
    cam = make_manager()
    applied = []
    monkeypatch.setattr(cam, "_apply_bitrate", lambda b: applied.append(b))
    assert cam.set_bitrate(50_000) == 500_000        # floor
    assert cam.set_bitrate(99_000_000) == 8_000_000  # configured ceiling
    assert applied == [500_000, 8_000_000]


def test_set_bitrate_noop_without_encoder(monkeypatch):
    cam = CameraManager({"camera": {"bitrate": 8_000_000}})
    monkeypatch.setattr(
        cam, "_apply_bitrate",
        lambda b: (_ for _ in ()).throw(AssertionError("must not be called")),
    )
    assert cam.set_bitrate(2_000_000) == 8_000_000  # unchanged, no ioctl


def test_set_bitrate_skips_redundant_changes(monkeypatch):
    cam = make_manager()
    applied = []
    monkeypatch.setattr(cam, "_apply_bitrate", lambda b: applied.append(b))
    cam.set_bitrate(2_000_000)
    cam.set_bitrate(2_000_000)  # same value: no second ioctl
    assert applied == [2_000_000]
