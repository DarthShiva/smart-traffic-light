"""
system_state.py
---------------
THE canonical state model. Everything the dashboard renders comes from
`SystemState.snapshot()`; there is no second representation of controller
state anywhere in the project, and no component transforms the schema on
the way past.

CANONICAL SNAPSHOT SCHEMA
=========================
{
  "server_time":   float   epoch seconds on the server, taken now
  "last_update":   float   alias of server_time, kept because the whole
                           countdown contract is (phase_deadline - last_update)
  "mode":          "auto" | "manual"
  "phase":         "green" | "all_red"
  "phase_started": float   epoch seconds
  "phase_deadline":float|null  epoch seconds; null means "holds until an
                           operator acts" (manual mode)
  "phase_total_sec": float|null  length of the current phase
  "active_lane":   "North"|"East"|"South"|"West"|null  (null during all-red)
  "signal_string": "GRRR"  exactly what was written to the serial port
  "cycle_number":  int     increments each time the rotation passes lane 1
  "phase_number":  int     monotonic counter of green phases served
  "lane_order":    ["North","East","South","West"]
  "lanes": {
     "<lane>": {
        "name": "North", "number": 1, "label": "Lane 1", "index": 0,
        "count": int,                    the count the controller uses
        "count_source": "vision"|"manual",
        "manual_count": int,             operator slider value
        "vision_count": int|null,        null when the lane is not being counted
        "signal": "R"|"G",               derived from signal_string
        "green_recommendation_sec": float   what it would get if green now
        "assigned_video": str|null,
        "connected": bool,               video source is producing frames
        "status": str,                   idle|opening|streaming|calibrating|error|unavailable
        "message": str,                  human-readable detail for the UI
        "fps": float, "source_fps": float, "loops": int,
        "frame_age_sec": float|null, "has_frame": bool
     }, ...
  }
  "decision":   {lane,count,green_sec,reason,rule,next_lane,capped,at}
  "serial":     see serial_link.ArduinoLink.status()
  "vision":     see vision.VisionManager.status()
  "videos":     ["lane1.mp4", ...]  assignable clips
  "comparison": null | simulation.benchmark() result
  "config":     {base_green_sec, per_vehicle_sec, max_green_sec, all_red_sec,
                 protocol: "R/G only - no amber in hardware or simulation"}
}
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Dict, Optional

import traffic_logic
from constants import (
    ALL_RED_SEC,
    BASE_GREEN_SEC,
    COUNT_SOURCE_MANUAL,
    COUNT_SOURCE_VISION,
    LANE_INDEX,
    LANE_NUMBER,
    LANES,
    MAX_GREEN_SEC,
    MODE_AUTO,
    MODE_MANUAL,
    PER_VEHICLE_SEC,
    PHASE_ALL_RED,
    PHASE_GREEN,
    lane_label,
)


class SystemState:
    """Controller state plus the composition of everybody else's state
    into one snapshot.

    All mutation goes through methods on this object, all of it under one
    reentrant lock. Vision keeps its own per-lane state (it is the owner of
    frames and detections) and is read here at snapshot time rather than
    being copied in continuously - which is what stops the two from
    drifting apart.
    """

    def __init__(self, vision, arduino, lanes=LANES,
                 on_phase_start: Optional[Callable[[dict], None]] = None,
                 on_phase_end: Optional[Callable[[dict], None]] = None):
        self.lanes = list(lanes)
        self.vision = vision
        self.arduino = arduino
        self._on_phase_start = on_phase_start
        self._on_phase_end = on_phase_end
        self.last_green_lane: Optional[str] = None

        self._lock = threading.RLock()
        self._manual_counts: Dict[str, int] = {lane: 0 for lane in self.lanes}

        self.mode = MODE_AUTO
        self.phase = PHASE_ALL_RED
        self.active_lane: Optional[str] = None
        self.signal_string = traffic_logic.all_red_string()
        self.cycle_number = 0
        self.phase_number = 0
        self.decision: Optional[dict] = None
        self.comparison: Optional[dict] = None

        now = time.time()
        self.phase_started = now
        self.phase_deadline: Optional[float] = None
        self.phase_total_sec: Optional[float] = None
        self._deadline_mono: Optional[float] = None

        self._skip_requested = False

    # ------------------------------------------------------------ counts --
    def set_manual_count(self, lane: str, value: object) -> int:
        with self._lock:
            self._manual_counts[lane] = traffic_logic.coerce_count(value)
            return self._manual_counts[lane]

    def effective_counts(self) -> Dict[str, int]:
        """What the controller plans with: the vision count when a lane is
        really being counted, otherwise the operator's manual value."""
        vision_report = self.vision.report() if self.vision else {}
        with self._lock:
            counts = {}
            for lane in self.lanes:
                report = vision_report.get(lane) or {}
                vision_count = report.get("vision_count")
                counts[lane] = (vision_count if vision_count is not None
                                else self._manual_counts.get(lane, 0))
            return counts

    def _count_source(self, report: dict) -> str:
        return (COUNT_SOURCE_VISION if report.get("vision_count") is not None
                else COUNT_SOURCE_MANUAL)

    # ------------------------------------------------------------- phase --
    def _apply_signal(self, active_lane: Optional[str], phase: str,
                      duration: Optional[float], decision: Optional[dict]) -> None:
        """Single writer for every field that describes 'what the lights are
        doing right now'. `duration=None` means the phase holds until an
        operator changes it."""
        now = time.time()
        with self._lock:
            ended = None
            if self.phase == PHASE_GREEN and self.active_lane is not None:
                ended = {
                    "lane": self.active_lane,
                    "count": (self.decision or {}).get("count", 0),
                    "allocated_sec": self.phase_total_sec,
                    "served_sec": max(0.0, now - self.phase_started),
                }

            self.active_lane = active_lane
            self.phase = phase
            self.signal_string = traffic_logic.signal_string(active_lane, self.lanes)
            self.phase_started = now
            self.phase_total_sec = duration
            self.phase_deadline = None if duration is None else now + duration
            self._deadline_mono = None if duration is None else time.monotonic() + duration
            if decision is not None:
                self.decision = decision
            if phase == PHASE_GREEN:
                self.phase_number += 1
                self.last_green_lane = active_lane
                if active_lane == self.lanes[0]:
                    self.cycle_number += 1
            self._skip_requested = False
            payload = {
                "lane": active_lane,
                "phase": phase,
                "signal_string": self.signal_string,
                "duration": duration,
                "count": (decision or {}).get("count", 0),
            }

        # Callbacks run outside the lock: they write to the serial port and
        # to disk, and must never be able to stall a snapshot request.
        if ended is not None and self._on_phase_end is not None:
            self._on_phase_end(ended)
        if self._on_phase_start is not None:
            self._on_phase_start(payload)

    def begin_green(self, lane: str, counts: Dict[str, int], duration: Optional[float] = None,
                    decision: Optional[dict] = None) -> None:
        if decision is None:
            count = traffic_logic.coerce_count(counts.get(lane, 0))
            decision = {
                "lane": lane, "count": count,
                "green_sec": traffic_logic.green_time_display(count),
                "rule": "manual override",
                "reason": f"Operator forced {lane_label(lane)} ({lane}) green in manual mode.",
                "next_lane": traffic_logic.next_lane(lane, self.lanes),
                "capped": False,
            }
        decision = dict(decision, at=time.time())
        self._apply_signal(lane, PHASE_GREEN, duration, decision)

    def begin_all_red(self, duration: Optional[float] = ALL_RED_SEC,
                      reason: Optional[str] = None) -> None:
        decision = None
        if reason is not None:
            with self._lock:
                previous = dict(self.decision) if self.decision else {}
            previous.update({"reason": reason, "at": time.time()})
            decision = previous
        self._apply_signal(None, PHASE_ALL_RED, duration, decision)

    def phase_expired(self) -> bool:
        with self._lock:
            if self._skip_requested:
                return True
            if self._deadline_mono is None:
                return False
            return time.monotonic() >= self._deadline_mono

    def request_skip(self) -> None:
        """Operator-triggered controlled interruption. This is the ONLY
        thing that ends a phase early - a vehicle count changing never
        does."""
        with self._lock:
            self._skip_requested = True

    def remaining_sec(self) -> Optional[float]:
        with self._lock:
            if self._deadline_mono is None:
                return None
            return max(0.0, self._deadline_mono - time.monotonic())

    def set_comparison(self, result: Optional[dict]) -> None:
        with self._lock:
            self.comparison = result

    def hold(self) -> None:
        """Drop the deadline so the current phase runs until an operator
        acts. Deliberately does NOT re-fire the phase callbacks - the
        lights did not change, only the timer did."""
        with self._lock:
            self.phase_deadline = None
            self._deadline_mono = None
            self.phase_total_sec = None
            self._skip_requested = False

    # -------------------------------------------------------------- mode --
    def set_mode(self, mode: str) -> None:
        """auto -> manual freezes the current phase; manual -> auto hands
        control back to the runner, which closes out the held phase and
        resumes the round robin from the last lane that had green."""
        with self._lock:
            previous = self.mode
            self.mode = mode
        if mode == MODE_MANUAL and previous != MODE_MANUAL:
            self.hold()
        elif mode == MODE_AUTO and previous != MODE_AUTO:
            self.request_skip()

    # ---------------------------------------------------------- snapshot --
    def snapshot(self) -> dict:
        vision_report = self.vision.report() if self.vision else {}
        vision_status = self.vision.status() if self.vision else {}
        serial_status = self.arduino.status() if self.arduino else {}
        videos = self.vision_videos()
        now = time.time()

        with self._lock:
            active = self.active_lane
            lanes_out = {}
            for lane in self.lanes:
                report = vision_report.get(lane) or {}
                source = self._count_source(report)
                vision_count = report.get("vision_count")
                manual_count = self._manual_counts.get(lane, 0)
                count = vision_count if source == COUNT_SOURCE_VISION else manual_count
                lanes_out[lane] = {
                    "name": lane,
                    "number": LANE_NUMBER[lane],
                    "index": LANE_INDEX[lane],
                    "label": lane_label(lane),
                    "count": count,
                    "count_source": source,
                    "manual_count": manual_count,
                    "vision_count": vision_count,
                    "signal": traffic_logic.signal_for_lane(lane, active),
                    "green_recommendation_sec": traffic_logic.green_time_display(count),
                    "assigned_video": report.get("assigned_video"),
                    "connected": bool(report.get("connected")),
                    "status": report.get("status", "unavailable"),
                    "message": report.get("message", ""),
                    "fps": report.get("fps", 0.0),
                    "source_fps": report.get("source_fps", 0.0),
                    "loops": report.get("loops", 0),
                    "frame_age_sec": report.get("frame_age_sec"),
                    "has_frame": bool(report.get("has_frame")),
                }

            return {
                "server_time": now,
                "last_update": now,
                "mode": self.mode,
                "phase": self.phase,
                "phase_started": self.phase_started,
                "phase_deadline": self.phase_deadline,
                "phase_total_sec": self.phase_total_sec,
                "active_lane": active,
                "signal_string": self.signal_string,
                "cycle_number": self.cycle_number,
                "phase_number": self.phase_number,
                "lane_order": list(self.lanes),
                "lanes": lanes_out,
                "decision": dict(self.decision) if self.decision else None,
                "serial": serial_status,
                "vision": vision_status,
                "videos": videos,
                "comparison": dict(self.comparison) if self.comparison else None,
                "config": {
                    "base_green_sec": BASE_GREEN_SEC,
                    "per_vehicle_sec": PER_VEHICLE_SEC,
                    "max_green_sec": MAX_GREEN_SEC,
                    "all_red_sec": ALL_RED_SEC,
                    "modes": [MODE_AUTO, MODE_MANUAL],
                    "protocol": "R/G only - the hardware has no amber, so neither does this system",
                },
            }

    def vision_videos(self):
        import vision  # local import keeps this module importable without OpenCV
        return vision.list_videos()
