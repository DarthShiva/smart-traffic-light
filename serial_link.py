"""
serial_link.py
--------------
Serial transport to the Arduino LED driver.

Protocol (must match arduino/traffic_led_driver_no_yellow.ino exactly):
    9600 baud, 8N1
    one line per update: <North><East><South><West>\\n
    each character is 'R' or 'G' - the firmware coerces anything else to
    red, so this module refuses to send a string that is not exactly
    NUM_LANES characters of R/G rather than letting the hardware silently
    disagree with the dashboard.

The firmware answers every accepted line with "Applied: <string>". We read
that back and expose it as `last_reply`, which is the only honest way for
the dashboard to claim the board actually received a command - "pyserial
is importable" and even "the port opened" prove nothing about the board.

Everything fails soft: with no pyserial, no board, or a yanked USB cable
the dashboard keeps running in no-hardware mode.
"""
from __future__ import annotations

import threading
import time
from typing import List, Optional, Tuple

from constants import (
    ARDUINO_BOOT_SEC,
    NUM_LANES,
    SERIAL_BAUDRATE,
    SERIAL_TIMEOUT_SEC,
    VALID_SIGNAL_CHARS,
)

try:
    import serial
    from serial.tools import list_ports
    PYSERIAL_AVAILABLE = True
except ImportError:  # pragma: no cover - depends on the host environment
    serial = None
    list_ports = None
    PYSERIAL_AVAILABLE = False


def is_valid_signal_string(text: object) -> bool:
    return (isinstance(text, str)
            and len(text) == NUM_LANES
            and all(ch in VALID_SIGNAL_CHARS for ch in text))


class ArduinoLink:
    """Thread-safe wrapper around one optional serial connection.

    Only one thread ever writes (the signal runner) but connect/disconnect
    arrive from request threads, so every touch of `_conn` is serialised by
    `_lock`.
    """

    def __init__(self, baudrate: int = SERIAL_BAUDRATE, timeout: float = SERIAL_TIMEOUT_SEC):
        self.baudrate = baudrate
        self.timeout = timeout
        self._lock = threading.RLock()
        self._conn = None
        self._port: Optional[str] = None
        self.last_error: Optional[str] = None
        self.last_sent: Optional[str] = None
        self.last_sent_at: Optional[float] = None
        self.last_reply: Optional[str] = None
        self.writes_ok = 0
        self.writes_failed = 0

    # ------------------------------------------------------------ state --
    @property
    def connected(self) -> bool:
        with self._lock:
            return self._conn is not None and bool(getattr(self._conn, "is_open", False))

    @property
    def port(self) -> Optional[str]:
        return self._port

    def available_ports(self) -> List[dict]:
        """[{'device': 'COM3', 'description': '...'}] - never raises.

        Works on Windows (COMn) and POSIX (/dev/tty*) alike; pyserial's
        comports() handles the platform difference for us.
        """
        if not PYSERIAL_AVAILABLE:
            return []
        try:
            return [{"device": p.device, "description": (p.description or "").strip()}
                    for p in list_ports.comports()]
        except Exception as exc:  # pragma: no cover - driver dependent
            self.last_error = f"port scan failed: {exc}"
            return []

    def status(self) -> dict:
        """The `serial` block of the canonical state snapshot."""
        connected = self.connected
        return {
            "pyserial_available": PYSERIAL_AVAILABLE,
            "connected": connected,
            "mode": "hardware" if connected else "no-hardware",
            "port": self._port,
            "last_error": self.last_error,
            "last_sent": self.last_sent,
            "last_sent_at": self.last_sent_at,
            "last_reply": self.last_reply,
            "writes_ok": self.writes_ok,
            "writes_failed": self.writes_failed,
        }

    # ------------------------------------------------------- connection --
    def connect(self, port: Optional[str] = None) -> Tuple[bool, str]:
        """Open a port. Returns (ok, message) - never raises."""
        if not PYSERIAL_AVAILABLE:
            self.last_error = "pyserial not installed"
            return False, "pyserial is not installed - run: pip install pyserial"

        if port is not None and not isinstance(port, str):
            return False, "port must be a string"
        port = (port or "").strip() or None

        with self._lock:
            self._close_locked()
            if port is None:
                devices = [p["device"] for p in self.available_ports()]
                port = devices[0] if devices else None
            if not port:
                self.last_error = "no serial ports detected"
                return False, "No serial ports detected. The dashboard keeps running without hardware."
            try:
                conn = serial.Serial(port, self.baudrate, timeout=self.timeout)
            except Exception as exc:
                self._conn = None
                self._port = None
                self.last_error = str(exc)
                return False, f"Could not open {port}: {exc}"

            self._conn = conn
            self._port = port
            self.last_error = None

        # The board resets on port open; wait for its bootloader outside the
        # lock so a slow board cannot stall a concurrent status request.
        time.sleep(ARDUINO_BOOT_SEC)
        with self._lock:
            if self._conn is conn:
                try:
                    conn.reset_input_buffer()
                except Exception:
                    pass
        return True, f"Connected to {port}"

    def disconnect(self) -> None:
        with self._lock:
            self._close_locked()

    def _close_locked(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
        self._conn = None
        self._port = None
        self.last_reply = None

    # ------------------------------------------------------------ write --
    def send_state(self, state_string: str) -> bool:
        """Write one R/G line. Returns True only if the bytes really went
        out. A failed write drops the link so the UI stops claiming the
        board is connected."""
        if not is_valid_signal_string(state_string):
            self.last_error = f"refused to send malformed state {state_string!r}"
            self.writes_failed += 1
            return False

        with self._lock:
            conn = self._conn
            if conn is None or not getattr(conn, "is_open", False):
                return False
            try:
                conn.write((state_string + "\n").encode("ascii"))
                conn.flush()
                self.last_sent = state_string
                self.last_sent_at = time.time()
                self.writes_ok += 1
            except Exception as exc:
                self.last_error = str(exc)
                self.writes_failed += 1
                self._close_locked()
                return False
            self._drain_replies_locked(conn)
        return True

    def _drain_replies_locked(self, conn) -> None:
        """Read whatever the firmware echoed back and keep the newest line.

        Also stops the OS receive buffer growing without bound over a long
        run, since the firmware prints on every accepted line.
        """
        try:
            while conn.in_waiting:
                raw = conn.readline()
                if not raw:
                    break
                text = raw.decode("ascii", errors="replace").strip()
                if text:
                    self.last_reply = text
        except Exception as exc:
            self.last_error = str(exc)
