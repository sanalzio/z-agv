from __future__ import annotations

import asyncio
import json
import os
import pty
import select
import socket
import struct
import sys
import threading
import time
import urllib.request
from pathlib import Path

import uvicorn
import websockets

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.server import app, parse_telemetry, validate_cmd  # noqa: E402


class MockLeonardo:
    """Simulates Arduino Leonardo Serial1 protocol over a Linux PTY."""

    def __init__(self) -> None:
        self.master_fd, self.slave_fd = pty.openpty()
        self.slave_name = os.ttyname(self.slave_fd)
        self.mode = "IDLE"
        self.estop = False
        self.lp = 0
        self.rp = 0
        self.drop_replies = False
        self.rx_commands: list[str] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)
        for fd in (self.master_fd, self.slave_fd):
            try:
                os.close(fd)
            except OSError:
                pass

    def _reply(self, text: str) -> None:
        if not self.drop_replies:
            os.write(self.master_fd, (text + "\n").encode("ascii"))

    def _tel(self) -> str:
        return f"TEL:{self.mode},{self.lp},{self.rp},00189100,-12"

    def _run(self) -> None:
        buf = bytearray()
        while not self._stop.is_set():
            ready, _, _ = select.select([self.master_fd], [], [], 0.05)
            if not ready:
                continue
            try:
                data = os.read(self.master_fd, 256)
            except OSError:
                return
            if not data:
                return
            buf.extend(data)
            while b"\n" in buf:
                idx = buf.find(b"\n")
                cmd = bytes(buf[:idx]).decode("ascii", errors="ignore").strip("\r ")
                del buf[: idx + 1]
                if not cmd:
                    continue
                self.rx_commands.append(cmd)
                if cmd == "P":
                    self._reply(self._tel())
                elif cmd == "E":
                    self.estop = True
                    self.mode = "ESTOP"
                    self.lp = self.rp = 0
                    self._reply("ACK:ESTOP")
                    self._reply(self._tel())
                elif cmd == "R":
                    self.estop = False
                    self.mode = "IDLE"
                    self.lp = self.rp = 0
                    self._reply("ACK:RESET")
                    self._reply(self._tel())
                elif cmd == "S":
                    if not self.estop:
                        self.mode = "IDLE"
                    self.lp = self.rp = 0
                    self._reply("ACK:S")
                    self._reply(self._tel())
                elif cmd in ("L", "F"):
                    if self.estop:
                        self._reply("ERR:ESTOP_LOCKED")
                    elif cmd == "L":
                        self.mode = "LINE"
                        self.lp, self.rp = 155, 145
                        self._reply("ACK:L")
                        self._reply(self._tel())
                    else:
                        self.mode = "MANUAL"
                        self.lp = self.rp = 160
                        self._reply("ACK:F")
                        self._reply(self._tel())


def test_unit_parsers() -> None:
    good = parse_telemetry("TEL:LINE,150,-120,00389200,-14")
    assert good == {
        "mode": "LINE",
        "left_pwm": 150,
        "right_pwm": -120,
        "sensors": [0, 0, 3, 8, 9, 2, 0, 0],
        "error": -14,
    }
    lost = parse_telemetry("TEL:IDLE,0,0,00000000,999")
    assert lost is not None and lost["error"] == 999

    for bad in (
        "TEL:BOGUS,0,0,00000000,0",
        "TEL:IDLE,0,0,0000000,0",
        "TEL:IDLE,0,0,0000000A,0",
        "TEL:IDLE,300,0,00000000,0",
        "TEL:IDLE,0,0,00000000,150",
        "ACK:ESTOP",
    ):
        assert parse_telemetry(bad) is None, f"Should reject {bad}"

    assert validate_cmd("  kp:1.25 ") == "kp:1.25"
    assert validate_cmd("") is None
    assert validate_cmd("A\nB") is None
    assert validate_cmd("X" * 32) is None


def test_offline_frontend_assets(base_url: str) -> None:
    bodies: dict[str, str] = {}
    for path in ("/", "/static/style.css", "/static/app.js", "/api/ports"):
        with urllib.request.urlopen(base_url + path, timeout=3) as resp:
            assert resp.status == 200
            body = resp.read().decode("utf-8")
            assert "http://" not in body and "https://" not in body, f"External URL found in {path}"
            bodies[path] = body
    assert 'rel="manifest" href="/manifest.webmanifest"' in bodies["/"]
    assert "beforeinstallprompt" not in bodies["/static/app.js"]


def test_pwa_manifest(base_url: str) -> None:
    with urllib.request.urlopen(base_url + "/manifest.webmanifest", timeout=3) as resp:
        assert resp.status == 200
        ctype = resp.headers.get("Content-Type", "")
        assert "application/manifest+json" in ctype, f"Unexpected manifest Content-Type: {ctype}"
        raw = resp.read().decode("utf-8")
        assert "http://" not in raw and "https://" not in raw
        manifest = json.loads(raw)

    assert manifest["display"] == "standalone"
    assert manifest["start_url"] == "/"
    assert manifest["scope"] == "/"
    assert manifest["background_color"] == "#1c1a18"
    assert manifest["theme_color"] == "#262320"
    assert len(manifest["icons"]) >= 3

    for icon in manifest["icons"]:
        with urllib.request.urlopen(base_url + icon["src"], timeout=3) as resp:
            assert resp.status == 200
            data = resp.read()
            assert len(data) > 0
            if icon["type"] == "image/png":
                assert data[:8] == b"\x89PNG\r\n\x1a\n"
                w, h = struct.unpack(">II", data[16:24])
                expected_w, expected_h = map(int, icon["sizes"].split("x"))
                assert (w, h) == (expected_w, expected_h), f"Bad PNG size for {icon['src']}: {(w, h)}"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def wait_for_msg(ws, predicate, timeout: float = 3.0) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        rem = deadline - time.monotonic()
        if rem <= 0:
            raise AssertionError("Timed out waiting for matching WebSocket message")
        raw = await asyncio.wait_for(ws.recv(), timeout=rem)
        msg = json.loads(raw)
        if predicate(msg):
            return msg


async def run_integration(port: int) -> None:
    base_url = f"http://127.0.0.1:{port}"
    ws_url = f"ws://127.0.0.1:{port}/ws"
    test_offline_frontend_assets(base_url)
    test_pwa_manifest(base_url)

    mock = MockLeonardo()
    mock.start()
    try:
        async with websockets.connect(ws_url) as ws:
            init_status = await wait_for_msg(ws, lambda m: m.get("type") == "status")
            assert init_status["connected"] is False
            await wait_for_msg(ws, lambda m: m.get("type") == "ports")

            await ws.send(json.dumps({"action": "connect", "port": mock.slave_name, "baud": 9600}))
            conn_status = await wait_for_msg(ws, lambda m: m.get("type") == "status" and m.get("connected") is True)
            assert conn_status["port"] == mock.slave_name

            tel1 = await wait_for_msg(ws, lambda m: m.get("type") == "telemetry")
            tel2 = await wait_for_msg(ws, lambda m: m.get("type") == "telemetry")
            assert tel1["mode"] == "IDLE" and tel1["sensors"] == [0, 0, 1, 8, 9, 1, 0, 0]
            assert isinstance(tel2["rtt_ms"], (int, float))

            await ws.send(json.dumps({"action": "cmd", "data": "E"}))
            await wait_for_msg(ws, lambda m: m.get("type") == "telemetry" and m.get("mode") == "ESTOP")

            await ws.send(json.dumps({"action": "cmd", "data": "L"}))
            await wait_for_msg(ws, lambda m: m.get("type") == "log" and m.get("data") == "ERR:ESTOP_LOCKED")

            await ws.send(json.dumps({"action": "cmd", "data": "R"}))
            await wait_for_msg(ws, lambda m: m.get("type") == "telemetry" and m.get("mode") == "IDLE")

            await ws.send(json.dumps({"action": "cmd", "data": "F"}))
            await wait_for_msg(ws, lambda m: m.get("type") == "telemetry" and m.get("mode") == "MANUAL")

        # Closing last WS client during MANUAL must transmit emergency stop "S"
        deadline = time.monotonic() + 1.0
        while "S" not in mock.rx_commands and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        assert "S" in mock.rx_commands, "Missing failsafe S on last WS disconnect in MANUAL mode"

        async with websockets.connect(ws_url) as ws:
            mock.drop_replies = True
            stale = await wait_for_msg(
                ws,
                lambda m: m.get("type") == "status" and m.get("link_alive") is False,
                timeout=2.5,
            )
            assert stale["connected"] is True and stale["link_alive"] is False
            mock.drop_replies = False
            await ws.send(json.dumps({"action": "disconnect"}))
            await wait_for_msg(ws, lambda m: m.get("type") == "status" and m.get("connected") is False)
    finally:
        mock.stop()


def main() -> None:
    test_unit_parsers()
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    srv_thread = threading.Thread(target=server.run, daemon=True)
    srv_thread.start()
    try:
        deadline = time.monotonic() + 5.0
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started, "Uvicorn server failed to start"
        asyncio.run(run_integration(port))
        print("ALL_BACKEND_TESTS_PASSED")
    finally:
        server.should_exit = True
        srv_thread.join(timeout=3.0)


if __name__ == "__main__":
    main()
