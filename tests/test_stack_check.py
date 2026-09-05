"""Tests for scripts/stack_check.py.

The probes are pointed at local stub servers rather than mocked, so the wire
formats they build - notably the hand-rolled MQTT CONNECT packet - are actually
exercised.
"""

import json
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import stack_check  # noqa: E402


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _StubBroker(threading.Thread):
    """Accepts one connection, validates CONNECT, replies with a CONNACK."""

    daemon = True

    def __init__(self, return_code: int = 0):
        super().__init__()
        self.return_code = return_code
        self.connect_packet = b""
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self.port = self._sock.getsockname()[1]

    def run(self):
        try:
            conn, _ = self._sock.accept()
            with conn:
                self.connect_packet = conn.recv(1024)
                conn.sendall(bytes([0x20, 0x02, 0x00, self.return_code]))
        except OSError:
            pass
        finally:
            self._sock.close()


def test_mqtt_connect_packet_is_well_formed():
    broker = _StubBroker()
    broker.start()

    ok, detail = stack_check.check_mqtt("127.0.0.1", broker.port, 3.0)
    broker.join(timeout=3)

    assert ok is True
    assert detail == "connection accepted"

    packet = broker.connect_packet
    assert packet[0] == 0x10, "must be an MQTT CONNECT control packet"
    # Variable header: 2-byte length, "MQTT", protocol level 4 (v3.1.1).
    assert packet[2:8] == b"\x00\x04MQTT"
    assert packet[8] == 0x04
    assert packet[9] & 0x02, "clean-session flag should be set"


def test_mqtt_credentials_set_username_and_password_flags():
    broker = _StubBroker()
    broker.start()

    stack_check.check_mqtt("127.0.0.1", broker.port, 3.0, "bob", "secret")
    broker.join(timeout=3)

    flags = broker.connect_packet[9]
    assert flags & 0x80, "username flag"
    assert flags & 0x40, "password flag"
    assert b"bob" in broker.connect_packet
    assert b"secret" in broker.connect_packet


@pytest.mark.parametrize(
    "code,expected",
    [
        (4, "refused: bad username or password"),
        (5, "refused: not authorised"),
    ],
)
def test_mqtt_auth_failure_is_reported_distinctly_from_being_down(code, expected):
    """A reachable broker rejecting credentials must not look like an outage."""
    broker = _StubBroker(return_code=code)
    broker.start()

    ok, detail = stack_check.check_mqtt("127.0.0.1", broker.port, 3.0)
    broker.join(timeout=3)

    assert ok is False
    assert detail == expected


def test_mqtt_reports_unreachable_when_nothing_is_listening():
    ok, detail = stack_check.check_mqtt("127.0.0.1", _free_port(), 1.0)
    assert ok is False
    assert "unreachable" in detail


def test_tcp_check_detects_closed_port():
    ok, _ = stack_check.check_tcp("127.0.0.1", _free_port(), 1.0)
    assert ok is False


def _serve(handler_cls):
    server = HTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_mcp_check_parses_sse_framed_initialize_response():
    """Streamable HTTP may answer as SSE, so JSON can arrive behind 'data:'."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {
                        "serverInfo": {"name": "test-server", "version": "9.9"}
                    },
                }
            )
            payload = f"event: message\ndata: {body}\n\n".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = _serve(Handler)
    try:
        port = server.server_address[1]
        ok, detail = stack_check.check_mcp(f"http://127.0.0.1:{port}/mcp", 3.0)
    finally:
        server.shutdown()

    assert ok is True
    assert detail == "test-server v9.9"


def test_comfyui_check_summarises_vram():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            payload = json.dumps(
                {
                    "devices": [
                        {
                            "name": "cuda:0 NVIDIA",
                            "vram_free": 8 * 1048576,
                            "vram_total": 16 * 1048576,
                        }
                    ]
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = _serve(Handler)
    try:
        port = server.server_address[1]
        ok, detail = stack_check.check_comfyui(f"http://127.0.0.1:{port}", 3.0)
    finally:
        server.shutdown()

    assert ok is True
    assert "8/16 MB VRAM free" in detail


def test_http_check_treats_unexpected_status_as_failure():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = _serve(Handler)
    try:
        port = server.server_address[1]
        ok, detail = stack_check.check_http(f"http://127.0.0.1:{port}/", timeout=3.0)
    finally:
        server.shutdown()

    assert ok is False
    assert "503" in detail


def test_failing_optional_check_does_not_set_exit_status():
    """Phase-3 services being absent must not make the report look broken."""
    argv = ["--host", "127.0.0.1", "--timeout", "0.2", "--only", "postgres", "--json"]
    assert stack_check.main(argv) == 0


def test_failing_required_check_sets_exit_status():
    argv = ["--host", "127.0.0.1", "--timeout", "0.2", "--only", "mosquitto", "--json"]
    assert stack_check.main(argv) == 1
