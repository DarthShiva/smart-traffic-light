"""
csv_logger.py
-------------
Append-only CSV log of every green phase, plus the aggregates the
dashboard's statistics panel shows.

Schema is unchanged from the original project so existing traffic_log.csv
files stay readable:

    Timestamp, Active Lane, Vehicle Count, Allocated Green Duration (s)

Hardening over the previous version: malformed rows (hand-edited files,
a crash mid-write) no longer raise ValueError out of get_stats() and take
the whole /api/logs endpoint down with them - they are skipped and
counted. Reads are also bounded so the panel does not get slower forever
as the log grows.
"""
from __future__ import annotations

import csv
import os
import threading
from datetime import datetime
from typing import List

from constants import BASE_DIR

LOG_FILE = os.path.join(BASE_DIR, "traffic_log.csv")
HEADER = ["Timestamp", "Active Lane", "Vehicle Count", "Allocated Green Duration (s)"]

# Cap on rows parsed per stats/recent call. A demo that runs for days
# should not turn a 3-second UI poll into a full-file scan.
MAX_ROWS_SCANNED = 5000

_lock = threading.RLock()


def ensure_log_file() -> None:
    with _lock:
        try:
            if not os.path.exists(LOG_FILE) or os.path.getsize(LOG_FILE) == 0:
                with open(LOG_FILE, "w", newline="", encoding="utf-8") as f:
                    csv.writer(f).writerow(HEADER)
        except OSError:
            pass  # a read-only working directory must not kill the server


def log_event(lane: str, count: int, duration: float) -> None:
    """One row per green phase. Called by the signal runner."""
    ensure_log_file()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _lock:
        try:
            with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow([timestamp, lane, int(count), round(float(duration), 1)])
        except OSError:
            pass


def _read_rows() -> List[list]:
    if not os.path.exists(LOG_FILE):
        return []
    with _lock:
        try:
            with open(LOG_FILE, "r", newline="", encoding="utf-8", errors="replace") as f:
                rows = list(csv.reader(f))
        except OSError:
            return []
    return rows[1:][-MAX_ROWS_SCANNED:]


def _is_data_row(row: list) -> bool:
    if len(row) != 4:
        return False
    try:
        float(row[2])
        float(row[3])
    except (TypeError, ValueError):
        return False
    return True


def get_recent_logs(num_entries: int = 10) -> List[list]:
    """Newest N valid rows, oldest of the batch first."""
    num_entries = max(1, min(MAX_ROWS_SCANNED, int(num_entries)))
    return [r for r in _read_rows() if _is_data_row(r)][-num_entries:]


def get_stats() -> dict:
    rows = _read_rows()
    data = [r for r in rows if _is_data_row(r)]
    skipped = len(rows) - len(data)
    if not data:
        return {"total_transitions": 0, "total_vehicles_logged": 0,
                "avg_green_time": 0.0, "per_lane": {}, "skipped_rows": skipped}

    per_lane: dict = {}
    total_vehicles = 0
    total_green = 0.0
    for row in data:
        vehicles = int(float(row[2]))
        green = float(row[3])
        total_vehicles += vehicles
        total_green += green
        bucket = per_lane.setdefault(row[1], {"transitions": 0, "vehicles": 0, "green_sec": 0.0})
        bucket["transitions"] += 1
        bucket["vehicles"] += vehicles
        bucket["green_sec"] = round(bucket["green_sec"] + green, 1)

    return {
        "total_transitions": len(data),
        "total_vehicles_logged": total_vehicles,
        "avg_green_time": round(total_green / len(data), 1),
        "per_lane": per_lane,
        "skipped_rows": skipped,
    }
