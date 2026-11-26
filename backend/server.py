from __future__ import annotations

import asyncio
import glob
import json
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import serial
import serial.tools.list_ports
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"

VALID_MODES = frozenset({"IDLE", "LINE", "TURN180", "MANUAL", "ESTOP"})
PING_INTERVAL_S = 0.10
PING_RETRY_S = 0.25
LINK_STALE_S = 1.20


def validate_cmd(raw: Any) -> str | None:
    """Validate an ASCII robot command before crossing the serial trust boundary."""
    if not isinstance(raw, str):
        return None
    cmd = raw.strip()
    if not (1 <= len(cmd) <= 31):
        return None
    if any(ord(ch) < 0x20 or ord(ch) > 0x7E for ch in cmd):
        return None
    return cmd


def parse_telemetry(line: str) -> dict[str, Any] | None:
    """Parse TEL:<MODE>,<LPWM>,<RPWM>,<S0..S7>,<ERR> into a validated dict."""
    if not line.startswith("TEL:"):
        return None
    parts = line[4:].split(",")
    if len(parts) != 5:
        return None
    mode, lp_s, rp_s, sensors_s, err_s = parts
    if mode not in VALID_MODES or len(sensors_s) != 8 or not sensors_s.isdigit():
        return None
    try:
        lp = int(lp_s)
        rp = int(rp_s)
        err = int(err_s)
    except ValueError:
        return None
    if not (-255 <= lp <= 255 and -255 <= rp <= 255):
        return None
    if not (-100 <= err <= 100 or err == 999):
        return None
    return {
        "mode": mode,
        "left_pwm": lp,
        "right_pwm": rp,
        "sensors": [int(ch) for ch in sensors_s],
        "error": err,
    }


def list_available_ports() -> list[dict[str, str]]:
    """Discover USB/hardware serial ports plus Linux Bluetooth /dev/rfcomm* nodes."""
    ports: dict[str, dict[str, str]] = {}
    for p in serial.tools.list_ports.comports():
        if p.device.startswith("/dev/ttyS") and (not p.hwid or p.hwid == "n/a"):
            continue
        ports[p.device] = {
            "device": p.device,
            "description": p.description or p.device,
        }
    for pattern, desc in (
        ("/dev/rfcomm*", "Bluetooth SPP (/dev/rfcomm)"),
        ("/dev/ttyACM*", "Arduino USB CDC"),
        ("/dev/ttyUSB*", "USB-Serial Adapter"),
    ):
        for path in sorted(glob.glob(pattern)):
            ports.setdefault(path, {"device": path, "description": desc})

    def sort_key(item: dict[str, str]) -> tuple[int, str]:
        dev = item["device"]
        if dev.startswith("/dev/rfcomm"):
            return (0, dev)
        if dev.startswith("/dev/ttyACM"):
            return (1, dev)
        if dev.startswith("/dev/ttyUSB"):
            return (2, dev)
        return (3, dev)

    return sorted(ports.values(), key=sort_key)


class ConnectionManager:
    """Broadcasts JSON messages to active WebSocket clients with single serialization."""

    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.clients.add(ws)

    def disconnect(self, ws: WebSocket) -> int:
        self.clients.discard(ws)
        return len(self.clients)

    async def send_one(self, ws: WebSocket, payload: dict[str, Any]) -> None:
        await ws.send_text(json.dumps(payload, separators=(",", ":")))

    async def broadcast(self, payload: dict[str, Any]) -> None:
        if not self.clients:
            return
        text = json.dumps(payload, separators=(",", ":"))
        targets = list(self.clients)
        results = await asyncio.gather(
            *(ws.send_text(text) for ws in targets),
            return_exceptions=True,
        )
        for ws, res in zip(targets, results):
            if isinstance(res, Exception):
                self.clients.discard(ws)


class SerialBridge:
    """Thread-safe serial bridge with non-blocking RX reader and priority TX cut-through."""

    def __init__(self) -> None:
        self.ser: serial.Serial | None = None
        self.port: str | None = None
        self.baud: int = 9600
        self.connected: bool = False
        self.link_alive: bool = False
        self.last_mode: str = "IDLE"
        self.last_rx_ts: float = 0.0
        self.last_ping_ts: float = 0.0
        self.last_tel_ts: float = 0.0
        self.pending_ping: bool = False
        self.rtt_ms: float | None = None
        self.tel_hz: float = 0.0
        self.last_telemetry: dict[str, Any] | None = None
        self.tx_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.reader_thread: threading.Thread | None = None

    def status_payload(self, reason: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "type": "status",
            "connected": self.connected,
            "link_alive": self.link_alive if self.connected else False,
            "port": self.port,
            "baud": self.baud,
            "mode": self.last_mode if self.connected else "DISCONNECTED",
            "rtt_ms": self.rtt_ms if self.connected else None,
            "hz": self.tel_hz if self.connected else 0.0,
        }
        if reason:
            payload["reason"] = reason
        return payload

    def connect(
        self,
        port: str,
        baud: int,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        clean_port = port.strip() if isinstance(port, str) else ""
        if not clean_port or len(clean_port) > 256:
            raise ValueError("Invalid serial port path")
        if not (1200 <= baud <= 115200):
            raise ValueError("Unsupported baud rate")

        self.disconnect()
        # shortcut: timeout=0.05 balances fast chunk wakeups vs CPU on Linux RFCOMM, switch to select/epoll only if multiple UARTs needed
        ser = serial.Serial(
            port=clean_port,
            baudrate=baud,
            timeout=0.05,
            write_timeout=0.5,
        )
        ser.reset_input_buffer()
        ser.reset_output_buffer()

        now = time.monotonic()
        self.ser = ser
        self.port = clean_port
        self.baud = baud
        self.connected = True
        self.link_alive = True
        self.last_mode = "IDLE"
        self.last_rx_ts = now
        self.last_ping_ts = 0.0
        self.last_tel_ts = 0.0
        self.pending_ping = False
        self.rtt_ms = None
        self.tel_hz = 0.0
        self.last_telemetry = None
        self.stop_event.clear()

        self.reader_thread = threading.Thread(
            target=self._reader_loop,
            args=(ser, loop),
            daemon=True,
            name="agv-serial-rx",
        )
        self.reader_thread.start()

    def _reader_loop(
        self,
        ser: serial.Serial,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        buf = bytearray()
        while not self.stop_event.is_set():
            try:
                waiting = ser.in_waiting
                chunk = ser.read(waiting if waiting > 0 else 1)
            except (serial.SerialException, OSError, TypeError, ValueError) as exc:
                if not self.stop_event.is_set():
                    asyncio.run_coroutine_threadsafe(_handle_serial_disconnect(f"Serial read error: {exc}"), loop)
                return

            if not chunk:
                continue

            buf.extend(chunk)
            if len(buf) > 2048:
                buf = buf[-1024:]

            while True:
                idx = buf.find(b"\n")
                if idx == -1:
                    break
                raw = bytes(buf[:idx])
                del buf[: idx + 1]
                line = raw.decode("ascii", errors="ignore").strip("\r\x00 ")
                if line:
                    asyncio.run_coroutine_threadsafe(_handle_serial_line(line), loop)

    def write_cmd(self, cmd: str, priority: bool = False) -> bool:
        clean = validate_cmd(cmd)
        if not clean or not self.connected:
            return False
        payload = (clean + "\n").encode("ascii")
        with self.tx_lock:
            ser = self.ser
            if ser is None or not ser.is_open:
                return False
            try:
                if priority:
                    ser.reset_output_buffer()
                ser.write(payload)
                ser.flush()
                return True
            except (serial.SerialException, OSError):
                return False

    def disconnect(self) -> None:
        if self.connected and self.last_mode == "MANUAL":
            self.write_cmd("S", priority=True)
        self.stop_event.set()
        ser = self.ser
        self.ser = None
        if ser is not None:
            try:
                ser.close()
            except OSError:
                pass
        thread = self.reader_thread
        self.reader_thread = None
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=0.5)
        self.connected = False
        self.link_alive = False
        self.pending_ping = False
        self.last_telemetry = None


manager = ConnectionManager()
bridge = SerialBridge()


def _record_ping_tx(now: float) -> None:
    bridge.last_ping_ts = now
    bridge.pending_ping = True


async def _handle_serial_line(line: str) -> None:
    now = time.monotonic()
    was_alive = bridge.link_alive
    bridge.last_rx_ts = now
    bridge.link_alive = True

    parsed = parse_telemetry(line)
    if parsed is not None:
        if bridge.pending_ping and bridge.last_ping_ts > 0:
            bridge.rtt_ms = round((now - bridge.last_ping_ts) * 1000.0, 1)
            bridge.pending_ping = False
        if bridge.last_tel_ts > 0:
            dt = now - bridge.last_tel_ts
            if 0.01 < dt < 2.0:
                inst_hz = 1.0 / dt
                bridge.tel_hz = round(
                    inst_hz if bridge.tel_hz == 0.0 else (0.7 * bridge.tel_hz + 0.3 * inst_hz),
                    1,
                )
        bridge.last_tel_ts = now
        bridge.last_mode = parsed["mode"]
        tel_msg = {
            "type": "telemetry",
            **parsed,
            "rtt_ms": bridge.rtt_ms,
            "hz": bridge.tel_hz,
            "link_alive": True,
        }
        bridge.last_telemetry = tel_msg
        if not was_alive:
            await manager.broadcast(bridge.status_payload())
        await manager.broadcast(tel_msg)
        return

    if not was_alive:
        await manager.broadcast(bridge.status_payload())
    await manager.broadcast({"type": "log", "dir": "rx", "data": line})


async def _handle_serial_disconnect(reason: str) -> None:
    if not bridge.connected and bridge.ser is None:
        return
    await asyncio.to_thread(bridge.disconnect)
    await manager.broadcast({"type": "log", "dir": "sys", "data": reason})
    await manager.broadcast(bridge.status_payload(reason=reason))


async def heartbeat_loop() -> None:
    while True:
        await asyncio.sleep(PING_INTERVAL_S)
        if not bridge.connected:
            continue
        now = time.monotonic()
        if bridge.link_alive and (now - bridge.last_rx_ts) > LINK_STALE_S:
            bridge.link_alive = False
            await manager.broadcast(bridge.status_payload(reason="Link timeout (E-Stop or BT lost)"))

        if not bridge.pending_ping or (now - bridge.last_ping_ts) >= PING_RETRY_S:
            _record_ping_tx(now)
            ok = await asyncio.to_thread(bridge.write_cmd, "P", False)
            if not ok and bridge.connected:
                await _handle_serial_disconnect("Bluetooth/Serial write failed")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    hb_task = asyncio.create_task(heartbeat_loop(), name="agv-heartbeat")
    try:
        yield
    finally:
        hb_task.cancel()
        try:
            await hb_task
        except asyncio.CancelledError:
            pass
        await asyncio.to_thread(bridge.disconnect)


app = FastAPI(title="Z-AGV Industry 4.0 SCADA Bridge", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/manifest.webmanifest")
async def manifest() -> FileResponse:
    return FileResponse(
        FRONTEND_DIR / "manifest.webmanifest",
        media_type="application/manifest+json",
    )


@app.get("/api/ports")
async def api_ports() -> dict[str, Any]:
    ports = await asyncio.to_thread(list_available_ports)
    return {"ports": ports}


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await manager.connect(ws)
    loop = asyncio.get_running_loop()

    try:
        await manager.send_one(ws, bridge.status_payload())
        ports = await asyncio.to_thread(list_available_ports)
        await manager.send_one(ws, {"type": "ports", "ports": ports})
        if bridge.last_telemetry is not None:
            await manager.send_one(ws, bridge.last_telemetry)

        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await manager.send_one(ws, {"type": "log", "dir": "err", "data": "Invalid JSON message"})
                continue

            action = msg.get("action")
            if action == "list_ports":
                ports = await asyncio.to_thread(list_available_ports)
                await manager.send_one(ws, {"type": "ports", "ports": ports})
            elif action == "connect":
                port = msg.get("port", "")
                baud = msg.get("baud", 9600)
                try:
                    baud_int = int(baud)
                except (TypeError, ValueError):
                    baud_int = 9600
                try:
                    await asyncio.to_thread(bridge.connect, port, baud_int, loop)
                except Exception as exc:
                    err_msg = f"Connect failed ({port}): {exc}"
                    await manager.send_one(ws, {"type": "log", "dir": "err", "data": err_msg})
                    await manager.broadcast(bridge.status_payload(reason=err_msg))
                    continue
                await manager.broadcast({"type": "log", "dir": "sys", "data": f"Connected to {bridge.port} @ {bridge.baud}"})
                await manager.broadcast(bridge.status_payload())
            elif action == "disconnect":
                await asyncio.to_thread(bridge.disconnect)
                await manager.broadcast({"type": "log", "dir": "sys", "data": "Serial disconnected"})
                await manager.broadcast(bridge.status_payload())
            elif action == "cmd":
                clean = validate_cmd(msg.get("data"))
                if not clean:
                    await manager.send_one(ws, {"type": "log", "dir": "err", "data": "Invalid command format"})
                    continue
                priority = clean in ("E", "S")
                if clean == "P":
                    _record_ping_tx(time.monotonic())
                ok = await asyncio.to_thread(bridge.write_cmd, clean, priority)
                if not ok:
                    await manager.send_one(ws, {"type": "log", "dir": "err", "data": f"TX failed (not connected): {clean}"})
                elif clean != "P":
                    await manager.broadcast({"type": "log", "dir": "tx", "data": clean})
            else:
                await manager.send_one(ws, {"type": "log", "dir": "err", "data": f"Unknown action: {action}"})
    except WebSocketDisconnect:
        pass
    finally:
        remaining = manager.disconnect(ws)
        # Failsafe: stop MANUAL drive immediately if the last operator UI drops
        if remaining == 0 and bridge.connected and bridge.last_mode == "MANUAL":
            bridge.write_cmd("S", priority=True)
            bridge.last_mode = "IDLE"


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=False)
