"""Localhost control endpoint for the camera service (Node → Python).

The HomeKit app owns the *policy* (it sees the sessions HomeKit negotiates);
this endpoint exposes the *mechanisms*:

    POST /bitrate   {"kbps": 2000}   →   {"applied_bps": 2000000}
    POST /keyframe                   →   {"forced": true}

Bound to 127.0.0.1 only — never reachable off-box, same posture as the
motion webhook on the Node side.
"""
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logger = logging.getLogger(__name__)


class ControlServer:
    def __init__(self, port: int, set_bitrate, force_keyframe=None):
        self._port = port
        self._set_bitrate = set_bitrate
        self._force_keyframe = force_keyframe
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        """Actual bound port (useful when constructed with port 0 in tests)."""
        return self._httpd.server_address[1] if self._httpd else self._port

    def start(self) -> None:
        set_bitrate = self._set_bitrate
        force_keyframe = self._force_keyframe

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                if self.path == "/bitrate":
                    try:
                        length = int(self.headers.get("Content-Length", 0))
                        kbps = int(json.loads(self.rfile.read(length))["kbps"])
                    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
                        self.send_error(400, "expected JSON body {\"kbps\": N}")
                        return
                    self._reply({"applied_bps": set_bitrate(kbps * 1000)})
                elif self.path == "/keyframe":
                    forced = bool(force_keyframe()) if force_keyframe else False
                    self._reply({"forced": forced})
                else:
                    self.send_error(404)

            def _reply(self, payload: dict):
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass  # keep journald quiet — the actions themselves are logged

        self._httpd = ThreadingHTTPServer(("127.0.0.1", self._port), Handler)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, daemon=True, name="control-server"
        )
        self._thread.start()
        logger.info("Control endpoint on http://127.0.0.1:%d", self.port)

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd = None
