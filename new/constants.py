"""
constants.py
------------
The ONE place where lane identity, signal characters and timing constants
are defined. Every other module imports from here; nothing re-declares a
lane list, a lane-number mapping or a green-time constant of its own.

Lane identity
=============
The canonical internal lane identifier is the compass name, because that
is what the Arduino firmware's pin table is ordered by
(traffic_control_arduino.ino -> lanePins[]: 0=North, 1=East,
2=South, 3=West). Using compass names internally means the signal string
this codebase produces is written to the wire with zero translation.

The UI speaks "Lane 1..4". That is a *display* concern and the mapping
lives here only:

    Lane 1 = North      Lane 2 = East
    Lane 3 = South      Lane 4 = West

Signal protocol
===============
The physical protocol is RED/GREEN only. The firmware accepts exactly two
characters, 'R' and 'G'; anything else is coerced to red by the firmware
itself. There is therefore NO amber/yellow state anywhere in this system -
not in the controller, not in the simulation, not in the UI. The
inter-green clearance interval is an ALL-RED phase ("RRRR"), which is a
real, representable hardware state.
"""
from __future__ import annotations

import os
from typing import Dict, List, Optional, Tuple

# --------------------------------------------------------------- lanes --
# Order is significant: index i of this tuple == index i of the signal
# string == lanePins[i] on the Arduino.
LANES: Tuple[str, ...] = ("North", "East", "South", "West")
NUM_LANES = len(LANES)

LANE_INDEX: Dict[str, int] = {lane: i for i, lane in enumerate(LANES)}
LANE_NUMBER: Dict[str, int] = {lane: i + 1 for i, lane in enumerate(LANES)}
LANE_BY_NUMBER: Dict[int, str] = {i + 1: lane for i, lane in enumerate(LANES)}


def lane_label(lane: str) -> str:
    """Display name used by the dashboard, e.g. 'Lane 1'."""
    return f"Lane {LANE_NUMBER[lane]}"


def resolve_lane(token: object) -> Optional[str]:
    """Map anything a client might send to a canonical lane name.

    Accepts 'North'/'north', 'Lane 1', 'lane1', '1', 1. Returns None when
    the token does not identify a lane, so callers can answer 400 instead
    of silently operating on the wrong lane.
    """
    if token is None:
        return None
    if isinstance(token, bool):  # bool is an int subclass - reject explicitly
        return None
    if isinstance(token, int):
        return LANE_BY_NUMBER.get(token)

    text = str(token).strip()
    if not text:
        return None
    for lane in LANES:
        if text.lower() == lane.lower():
            return lane
    digits = text.lower().replace("lane", "").replace("_", "").replace("-", "").strip()
    if digits.isdigit():
        return LANE_BY_NUMBER.get(int(digits))
    return None


# -------------------------------------------------------------- signals --
SIGNAL_RED = "R"
SIGNAL_GREEN = "G"
VALID_SIGNAL_CHARS = frozenset((SIGNAL_RED, SIGNAL_GREEN))
ALL_RED_STRING = SIGNAL_RED * NUM_LANES

PHASE_GREEN = "green"
PHASE_ALL_RED = "all_red"

MODE_AUTO = "auto"
MODE_MANUAL = "manual"
VALID_MODES = (MODE_AUTO, MODE_MANUAL)

COUNT_SOURCE_VISION = "vision"
COUNT_SOURCE_MANUAL = "manual"

# --------------------------------------------------------------- timing --
# Green time = BASE_GREEN_SEC + PER_VEHICLE_SEC * count, capped at
# MAX_GREEN_SEC. This is the team's algorithm; see traffic_logic.green_time
# for the single implementation.
BASE_GREEN_SEC = 10.0
PER_VEHICLE_SEC = 0.5
MAX_GREEN_SEC = 60.0

# Clearance interval between two greens. Sent to the hardware as "RRRR",
# which the firmware supports natively.
ALL_RED_SEC = 2.0

MAX_COUNT = 999  # defensive clamp on any externally supplied vehicle count

# ---------------------------------------------------------------- paths --
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
VIDEO_DIRS = (BASE_DIR, os.path.join(BASE_DIR, "videos"))
VIDEO_EXTENSIONS = (".mp4", ".avi", ".mov", ".mkv", ".webm")

# Uploads land here, and here only. It is already one of VIDEO_DIRS, so an
# accepted upload joins the normal catalogue and is assigned through the
# same basename whitelist as every other clip.
UPLOAD_DIR = os.path.join(BASE_DIR, "videos")
MAX_UPLOAD_BYTES = 512 * 1024 * 1024

# Lane -> default clip, used on first boot only. Reassignment is a runtime
# operation (POST /api/lane/<lane>/video).
DEFAULT_LANE_VIDEOS: Dict[str, str] = {
    "North": "lane1.mp4",
    "East": "lane2.mp4",
    "South": "lane3.mp4",
    "West": "lane4.mp4",
}

# --------------------------------------------------------------- vision --
# COCO class ids that count as a vehicle: car, motorcycle, bus, truck.
VEHICLE_CLASS_IDS = frozenset((2, 3, 5, 7))
DETECTION_CONFIDENCE = 0.40
DETECTION_IMGSZ = 480
FRAME_WIDTH = 640
FRAME_HEIGHT = 360
TARGET_DISPLAY_FPS = 8.0
JPEG_QUALITY = 72
# A lane is "connected" only if it produced a decoded frame this recently.
FRAME_STALE_SEC = 3.0
# Stop spending CPU on JPEG encoding when nobody has asked for frames.
VIEWER_IDLE_SEC = 10.0
# Exponential moving average applied to raw per-frame detections.
COUNT_SMOOTHING = 0.35

# --------------------------------------------------------------- serial --
SERIAL_BAUDRATE = 9600
SERIAL_TIMEOUT_SEC = 1.0
# The firmware resets when the port is opened; it needs this long to boot
# before it will read anything we send.
ARDUINO_BOOT_SEC = 2.0

# ----------------------------------------------------------- benchmark --
# Fixed-timer baseline used by the simulated benchmark (simulation.py).
FIXED_TIMER_GREEN_SEC = 30.0


def lane_list() -> List[str]:
    return list(LANES)
