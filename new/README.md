# Smart Traffic Control — Dashboard

A 4-lane adaptive traffic signal controller. A Flask backend counts
vehicles from four video feeds with YOLOv8, allocates green time from
those counts, drives an Arduino LED rig over serial, logs every
completed green to CSV, and streams the whole state to a browser
dashboard that can also drive the intersection by hand.

It runs with **no Arduino and no YOLO installed**. Both degrade
explicitly rather than silently: the dashboard says `no-hardware` and
`vision unavailable` instead of pretending.

## 1. Install & run

```bash
pip install -r requirements.txt
```

```bash
python app.py
```

Open **http://localhost:5000**. The signal cycle starts immediately.

`requirements.txt` is annotated per block: Flask is required, and
`pyserial`, `opencv-python` and `ultralytics`/`torch` are each optional
with a comment stating exactly what stops working without them.

For live counting you also need `lane1.mp4` … `lane4.mp4` and
`yolov8n.pt` beside `app.py` (all present in this directory). Without
OpenCV the camera wall shows `NO SIGNAL` and every lane's count falls
back to the operator's slider.

## 2. Project structure

```
traffic-dashboard/
├── app.py             Flask: routes, SSE, signal-runner thread
├── constants.py       Lane names, timing, thresholds, lane resolution
├── traffic_logic.py   THE algorithm: round-robin order + green duration
├── system_state.py    The one canonical state object and its snapshot
├── vision.py          Per-lane capture/decode workers + shared detector
├── serial_link.py     Arduino transport; fails soft with no board
├── simulation.py      Offline fixed-vs-adaptive benchmark
├── csv_logger.py      One row per completed green
├── arduino/traffic_led_driver_no_yellow.ino
├── static/{app.js,style.css}
├── templates/dashboard.html
├── tests/             100 tests, stdlib unittest only
└── legacy/            Retired code kept for reference, not imported
```

## 3. The algorithm

There is exactly one implementation, in `traffic_logic.py`. The live
backend, the simulation, and the recommendation shown in the browser all
call it — nothing recomputes green time locally.

**Order is a fixed round robin** and does not depend on traffic:

```
North (Lane 1) → East (Lane 2) → South (Lane 3) → West (Lane 4) → North …
```

A lane with 500 vehicles waiting does **not** jump the queue. Only the
*duration* responds to demand:

```
green_sec = min(60, 10 + 0.5 * vehicle_count)
```

| count | 0  | 1    | 10 | 40 | 99   | 100 | 1000 |
|-------|----|------|----|----|------|-----|------|
| green | 10 | 10.5 | 15 | 30 | 59.5 | 60  | 60   |

Negative, non-numeric and non-finite counts floor to the 10 s base
rather than raising. Every green is separated by a 2 s all-red
clearance. **There is no amber phase anywhere** — the firmware has no
amber LED, so displaying one would be a lie.

A lane whose video is detached still gets its turn in the rotation, at
the 10 s base.

## 4. How a count becomes a green light

1. **Decode.** One thread per lane owns one `cv2.VideoCapture`. It
   loops the clip at EOF and re-opens on a decode failure. No other
   thread ever touches that capture; assignment and recalibration are
   handed to the owning thread through a command queue.
2. **Detect.** A single shared thread runs YOLOv8n over the newest frame
   of each lane in turn (COCO classes car/motorcycle/bus/truck, conf
   0.40) and publishes the count. One detector, not four, so the GPU/CPU
   cost does not scale with lane count.
3. **Publish, don't interrupt.** `publish_detection()` only updates the
   number. It cannot end the running phase. A count that changes during
   a green is picked up by the *next* planning decision. The only
   early-termination path in the system is the operator pressing
   RECALCULATE (`POST /api/recalculate`).
4. **Plan.** When the phase deadline passes, the runner snapshots the
   counts, asks `traffic_logic.plan_next_green()` for the successor lane
   and its duration, and starts that phase.
5. **Drive.** Each phase change writes one line to the serial port:
   `<North><East><South><West>\n`, e.g. `GRRR\n`. With no board attached
   the write is skipped and reported honestly.
6. **Log.** Each completed green appends `timestamp, lane, count,
   duration` to `traffic_log.csv` — the duration *served*, not the one
   allocated, so a skipped phase logs what actually happened.
7. **Stream.** One SSE connection per tab (`/api/stream`) pushes the
   whole snapshot roughly once a second. The countdown is interpolated
   locally from `phase_deadline - last_update` at 5 Hz, so it ticks
   smoothly between pushes without drifting from the server, going
   negative, or jumping when a frame arrives.

SSE is the **only** channel carrying state: one `EventSource`, guarded
against duplicate connections, relying on the browser's own reconnect
plus a `retry: 2000` directive. There is a single `/api/data` fetch at
boot so the page renders before the first SSE frame, and a 4 s poll of
`/api/logs`, which SSE deliberately does not carry — re-reading the CSV
once per second per client would be wasteful. No polling competes with
the stream for state.

## 5. State schema

`GET /api/data` and every SSE frame carry the same object. This is the
canonical schema; nothing in the codebase reads any other shape.

```jsonc
{
  "mode": "auto" | "manual",
  "phase": "green" | "all_red",
  "active_lane": "North" | null,
  "signal_string": "GRRR",
  "phase_started": 1787390622.01,
  "phase_deadline": 1787390634.06,   // epoch seconds, null = holds
  "phase_total_sec": 12.0,
  "server_time": 1787390628.44,
  "last_update": 1787390628.44,      // server clock, anchors the countdown
  "cycle_number": 7,
  "phase_number": 29,
  "lane_order": ["North", "East", "South", "West"],
  "videos": ["lane1.mp4", "lane2.mp4", "lane3.mp4", "lane4.mp4"],
  "config": { "base_green_sec": 10.0, "per_vehicle_sec": 0.5,
              "max_green_sec": 60.0, "all_red_sec": 2.0, ... },
  "decision": { "lane": "East", "count": 4, "green_sec": 12.0,
                "signal": "RGRR", "rule": "...", "reason": "...",
                "next_lane": "South", "capped": false, "at": 1787390622.01 },
  "serial": { "pyserial_available": true, "connected": false,
              "mode": "no-hardware", "port": null, "last_error": null,
              "last_sent": null, "last_sent_at": null, "last_reply": null,
              "writes_ok": 0, "writes_failed": 0 },
  "vision": { "opencv_available": true, "detector_available": true,
              "detector": "yolov8n.pt", "detector_status": "...",
              "workers_alive": 4, "workers_expected": 4 },
  "comparison": null,                // populated by POST /api/simulate
  "lanes": {
    "North": {
      "name": "North", "label": "Lane 1", "number": 1, "index": 0,
      "count": 4,
      "vision_count": 4,             // null when the detector isn't counting
      "manual_count": 0,
      "count_source": "vision" | "manual",
      "signal": "G" | "R",
      "connected": true,
      "has_frame": true,
      "assigned_video": "lane1.mp4",
      "green_recommendation_sec": 12.0,
      "status": "...", "message": "...",
      "fps": 24.9, "source_fps": 25.0,
      "frame_age_sec": 0.12, "loops": 3
    }
  }
}
```

Lane keys are always `North`/`East`/`South`/`West` internally. `Lane 1..4`
exists only as the `label` field for display. Request parameters accept
either form (`North`, `north`, `lane1`, `Lane 1`, `1`) through one
`resolve_lane()` helper.

## 6. API reference

Every route the server exposes, and nothing else. `/api/state`,
`/media/<file>` and `/video_feed/<n>` are **retired** and there is a test
asserting they stay gone.

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/` | Dashboard page |
| GET | `/api/data` | Full canonical state, one shot |
| GET | `/api/stream` | Same object over SSE, ~1/s, auto-reconnecting |
| GET | `/api/frame/<lane>` | Latest cached JPEG for one lane (404 if none) |
| POST | `/api/counts` | `{"North": 12}` — manual counts; ignored for lanes on vision |
| POST | `/api/recalculate` | The one legitimate way to cut a phase short |
| POST | `/api/mode` | `{"mode": "auto"\|"manual"}` |
| POST | `/api/manual/set_lane` | `{"lane": "East"}` or `{"lane": null}` for all-red |
| GET | `/api/videos` | Whitelisted clip names + current assignments |
| POST | `/api/lane/<lane>/video` | `{"video": "lane3.mp4"}` or `null` to detach |
| POST | `/api/lane/<lane>/recalibrate` | Reopen one lane's capture |
| POST | `/api/recalibrate` | Reopen all four |
| GET | `/api/serial/ports` | Detected ports (empty without pyserial) |
| POST | `/api/serial/connect` | `{"port": "COM3"}` or omit to auto-pick |
| POST | `/api/serial/disconnect` | Drop the link |
| POST | `/api/simulate` | Run the offline benchmark |
| DELETE | `/api/simulate` | Clear the benchmark result |
| GET | `/api/logs?n=12` | Recent CSV rows + aggregate stats |

Errors return `{"ok": false, "error": "<message the operator can read>"}`
with a 4xx status. The dashboard surfaces that message in its alert bar
instead of failing silently.

Video names are validated against a **basename whitelist** of the clips
actually present. Paths, traversal (`../app.py`, `C:\Windows\win.ini`),
NUL bytes and non-strings are all rejected; no request can name an
arbitrary file on disk.

## 7. Arduino

Protocol, pin map and firmware behaviour are unchanged:

```
9600 baud, 8N1
one line per phase change: <North><East><South><West>\n
each character R or G — the firmware coerces anything else to red
the firmware answers "Applied: <line>"
```

| Lane | Red pin | Green pin |
|------|---------|-----------|
| North | 2 | 3 |
| East | 4 | 5 |
| South | 6 | 7 |
| West | 8 | 9 |

The dashboard reports `connected` **only** when a port is genuinely
open, and exposes `last_reply` — the firmware's own echo — because that
is the only evidence the board actually received anything. "pyserial is
importable" and even "the port opened" prove nothing.

A failed write drops the link rather than leaving the UI claiming
hardware. Malformed frames are refused before the wire.

One full cycle looks like this on the port (captured from the test
harness in §9):

```
RRRR  GRRR  RRRR  RGRR  RRRR  RRGR  RRRR  RRRG  RRRR
```

## 8. Benchmark (fixed timer vs adaptive)

`POST /api/simulate` runs an **offline** discrete-event model in
`simulation.py`. It never touches live state, never writes to the serial
port, and never calls a live endpoint — there is a test that asserts all
three by diffing the snapshot and the serial write counter around a run.

- Poisson arrivals, one arrival stream generated per seed and **replayed
  for both strategies**, so the two are compared on identical traffic.
- 0.5 veh/s saturation flow, 2 s all-red clearance for both.
- The adaptive branch calls the same `traffic_logic.green_time()` the
  live controller uses. A test re-derives every green in the returned
  trace from the queue length recorded at that phase's start.
- Units are **vehicle-seconds of cumulative waiting time**, stated in
  the response and printed under the numbers in the UI.
- `pct_saved` is 0.0, not 100 and not NaN, when the fixed total is zero.

These are model outputs, not measurements of real traffic, and the UI
labels them as such.

## 9. Tests

```bash
python tests/test_traffic_logic.py
```

```bash
python tests/test_security_and_sim.py
```

```bash
python tests/test_serial_protocol.py
```

```bash
python tests/test_api.py
```

100 tests, stdlib `unittest` only, no network and no hardware.

- `test_traffic_logic.py` — green-time boundaries (0, 1, 10, 40, 99,
  100, 1000, negative, non-integer, junk), round-robin order, the
  "a big queue must not jump the queue" case, R/G-only signal strings.
- `test_security_and_sim.py` — path-traversal rejection, serial frame
  validation, simulation determinism and honesty.
- `test_serial_protocol.py` — substitutes a recording stand-in for
  `serial.Serial` and asserts the **exact bytes** an Arduino would
  receive across a full cycle, plus reply readback, write failure,
  reconnect, and concurrent writers.
- `test_api.py` — every route, invalid input, phase transitions, video
  assignment and detachment, recalibration, mode switching. It parks
  the signal runner and redirects the CSV to a temp file so it cannot
  perturb the running system or the real log.
