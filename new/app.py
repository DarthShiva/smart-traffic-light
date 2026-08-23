"""
app.py
------
Flask backend for the 4-lane adaptive traffic dashboard.

    4 video files -> vision.LaneStream x4 -> vehicle counts
                                                  |
                          traffic_logic (round robin + 10 + 0.5*count, max 60)
                                                  |
                     system_state.SystemState (the one state model)
                            |                     |
                   serial_link -> Arduino    /api/data + /api/stream -> browser

Design rules this file follows:

*  The signal runner is the ONLY thread that changes the lights in auto
   mode. Vision threads publish counts and nothing else - a count changing
   never ends a green early (that was the old `force_recalc` bug). The
   runner reads the freshest counts at each phase boundary.
*  Every response the dashboard consumes is `SystemState.snapshot()`,
   unmodified. No route reshapes the schema.
*  Nothing here needs an Arduino. Serial output is a no-op when nothing is
   connected, and the UI says "no hardware" rather than pretending.

Run:  pip install -r requirements.txt && python app.py   ->  http://localhost:5000
"""
from __future__ import annotations

import json
import os
import threading
import time

from flask import (Flask, Response, jsonify, render_template, request,
                   stream_with_context)
from werkzeug.utils import secure_filename

import csv_logger
import simulation
import traffic_logic
import vision
from constants import (ALL_RED_SEC, BASE_DIR, DEFAULT_LANE_VIDEOS, LANES,
                       MAX_GREEN_SEC, MAX_UPLOAD_BYTES, MODE_AUTO, MODE_MANUAL,
                       PHASE_GREEN, UPLOAD_DIR, VALID_MODES, VIDEO_EXTENSIONS,
                       resolve_lane)
from serial_link import ArduinoLink
from system_state import SystemState
from vision import VisionManager

WEIGHTS_PATH = os.path.join(BASE_DIR, "yolov8n.pt")
RUNNER_TICK_SEC = 0.05
SSE_INTERVAL_SEC = 1.0

app = Flask(__name__)
# Werkzeug rejects a larger body before it reaches the route, so a huge
# upload cannot fill the disk while we are deciding whether to keep it.
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES

arduino = ArduinoLink()
vision_manager = VisionManager(WEIGHTS_PATH)


def _on_phase_start(payload: dict) -> None:
    """Push the new R/G string to the hardware. Called by SystemState for
    every phase change, including all-red."""
    arduino.send_state(payload["signal_string"])


def _on_phase_end(payload: dict) -> None:
    """One CSV row per completed green, with the duration actually served
    (which is what the operator saw) rather than the one allocated - they
    differ whenever a phase is skipped or held in manual mode."""
    csv_logger.log_event(payload["lane"], payload["count"], payload["served_sec"])


state = SystemState(vision_manager, arduino, LANES,
                    on_phase_start=_on_phase_start, on_phase_end=_on_phase_end)

_shutdown = threading.Event()


# ------------------------------------------------------------- runner ----
def signal_runner() -> None:
    """Drives the phase state machine in auto mode.

    green -> all-red -> next lane's green -> ... The next lane is always
    the round-robin successor; only the DURATION depends on traffic.
    """
    state.begin_all_red(ALL_RED_SEC)
    while not _shutdown.is_set():
        _shutdown.wait(RUNNER_TICK_SEC)
        try:
            if state.mode != MODE_AUTO:
                continue  # manual mode: phases change only when an operator says so
            if not state.phase_expired():
                continue
            if state.phase == PHASE_GREEN:
                state.begin_all_red(ALL_RED_SEC)
            else:
                counts = state.effective_counts()
                decision = traffic_logic.plan_next_green(state.last_green_lane, counts, LANES)
                state.begin_green(decision["lane"], counts, decision["green_sec"], decision)
        except Exception as exc:  # the runner must survive anything
            app.logger.exception("signal runner error: %s", exc)
            _shutdown.wait(1.0)


# -------------------------------------------------------------- helpers --
def _json_body() -> dict:
    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else {}


def _number(value, field: str, low: float, high: float, integer: bool = False) -> float:
    """Parse and range-check one request parameter. Raises ValueError with a
    message the operator will actually see in the alert bar."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"{field} must be a number")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be a number, got {value!r}")
    if number != number or number in (float("inf"), float("-inf")):
        raise ValueError(f"{field} must be a finite number")
    if not (low <= number <= high):
        raise ValueError(f"{field} must be between {low:g} and {high:g}, got {number:g}")
    return int(number) if integer else number


def _lane_or_400(token):
    lane = resolve_lane(token)
    if lane is None:
        return None, (jsonify({"ok": False,
                               "error": f"unknown lane {token!r}",
                               "valid": [f"{i + 1}" for i in range(len(LANES))] + list(LANES)}), 400)
    return lane, None


# --------------------------------------------------------------- pages --
@app.route("/")
def index():
    return render_template("dashboard.html")


# ----------------------------------------------------------- live state --
@app.route("/api/data")
def api_data():
    """The canonical snapshot. Documented in system_state.py."""
    return jsonify(state.snapshot())


@app.route("/api/stream")
def api_stream():
    """Server-Sent Events: the dashboard's primary live channel. One
    connection per tab, re-established automatically by EventSource."""
    def generate():
        yield "retry: 2000\n\n"
        while not _shutdown.is_set():
            try:
                yield f"data: {json.dumps(state.snapshot())}\n\n"
            except GeneratorExit:  # browser closed the tab
                return
            time.sleep(SSE_INTERVAL_SEC)

    return Response(stream_with_context(generate()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "Connection": "keep-alive",
                             "X-Accel-Buffering": "no"})


@app.route("/api/frame/<lane>")
def api_frame(lane):
    """Latest JPEG for one lane, served straight from the worker's cache -
    no encoding happens on this request path."""
    lane_name, error = _lane_or_400(lane)
    if error:
        return error
    data = vision_manager.frame(lane_name)
    if data is None:
        report = vision_manager.report().get(lane_name, {})
        return jsonify({"ok": False, "lane": lane_name,
                        "status": report.get("status", "unavailable"),
                        "error": report.get("message") or "no frame available"}), 404
    return Response(data, mimetype="image/jpeg",
                    headers={"Cache-Control": "no-store, max-age=0"})


# ----------------------------------------------------------- operation --
@app.route("/api/counts", methods=["POST"])
def api_counts():
    """{"North": 12, "2": 4} - manual vehicle counts. These are only used
    by lanes whose count_source is "manual" (no video, or vision
    unavailable); a lane being counted by the detector ignores them, and
    the dashboard disables its slider to match."""
    payload = _json_body()
    updated, rejected = {}, []
    for key, value in payload.items():
        lane = resolve_lane(key)
        if lane is None:
            rejected.append(key)
            continue
        updated[lane] = state.set_manual_count(lane, value)
    if not updated and rejected:
        return jsonify({"ok": False, "error": "no valid lanes in request",
                        "rejected": rejected}), 400
    return jsonify({"ok": True, "updated": updated, "rejected": rejected})


@app.route("/api/recalculate", methods=["POST"])
def api_recalculate():
    """Operator-triggered controlled interruption: end the current phase
    now. The next phase is the round-robin successor, timed from the
    counts as they are at that moment. This is the only thing that cuts a
    green short."""
    if state.mode != MODE_AUTO:
        return jsonify({"ok": False, "error": "switch to auto mode to recalculate"}), 409
    state.request_skip()
    return jsonify({"ok": True, "message": "current phase ended - recomputing from live counts"})


@app.route("/api/mode", methods=["POST"])
def api_mode():
    mode = _json_body().get("mode")
    if mode not in VALID_MODES:
        return jsonify({"ok": False, "error": f"mode must be one of {list(VALID_MODES)}"}), 400
    state.set_mode(mode)
    return jsonify({"ok": True, "mode": mode})


@app.route("/api/manual/set_lane", methods=["POST"])
def api_manual_set_lane():
    """Manual mode only. {"lane": "North"} holds that lane green;
    {"lane": null} holds all-red."""
    if state.mode != MODE_MANUAL:
        return jsonify({"ok": False, "error": "manual control requires manual mode"}), 409

    token = _json_body().get("lane")
    if token is None or (isinstance(token, str) and token.strip().lower() in ("", "none", "all_red")):
        state.begin_all_red(duration=None, reason="Operator held all-red (manual mode).")
        return jsonify({"ok": True, "active_lane": None})

    lane, error = _lane_or_400(token)
    if error:
        return error
    counts = state.effective_counts()
    state.begin_green(lane, counts, duration=None)
    return jsonify({"ok": True, "active_lane": lane})


# --------------------------------------------------------------- video --
@app.route("/api/videos")
def api_videos():
    report = vision_manager.report()
    return jsonify({
        "ok": True,
        "videos": vision.list_videos(),
        "assignments": {lane: report[lane]["assigned_video"] for lane in LANES},
    })


@app.route("/api/videos/upload", methods=["POST"])
def api_videos_upload():
    """multipart/form-data: `video` = the file, optional `lane` to assign it to.

    The uploaded name is never trusted. It is reduced to a safe basename,
    checked against the same extension whitelist the catalogue uses, and
    written into UPLOAD_DIR - one of the directories vision.py already
    scans, so the clip joins the normal catalogue and every later
    assignment goes through the existing basename whitelist. The final
    path is re-checked to be a direct child of UPLOAD_DIR, so no crafted
    name (traversal, absolute path, NUL byte) can place a file elsewhere.
    """
    upload = request.files.get("video")
    if upload is None or not upload.filename:
        return jsonify({"ok": False, "error": "no file was uploaded (field name: 'video')"}), 400

    lane_name = None
    requested_lane = request.form.get("lane")
    if requested_lane not in (None, "", "none"):
        lane_name = resolve_lane(requested_lane)
        if lane_name is None:
            return jsonify({"ok": False, "error": f"unknown lane {requested_lane!r}"}), 400

    name = secure_filename(os.path.basename(upload.filename or ""))
    if not name or name.startswith("."):
        return jsonify({"ok": False, "error": "that filename is not usable"}), 400
    if not name.lower().endswith(VIDEO_EXTENSIONS):
        return jsonify({"ok": False,
                        "error": f"unsupported file type - allowed: {', '.join(VIDEO_EXTENSIONS)}"}), 400

    try:
        os.makedirs(UPLOAD_DIR, exist_ok=True)
    except OSError as exc:
        return jsonify({"ok": False, "error": f"could not create the upload folder: {exc}"}), 500

    # Never overwrite an existing clip - a lane could be decoding it right now.
    stem, extension = os.path.splitext(name)
    candidate, suffix = name, 1
    while os.path.exists(os.path.join(UPLOAD_DIR, candidate)) or vision.resolve_video(candidate):
        candidate = f"{stem}_{suffix}{extension}"
        suffix += 1
    destination = os.path.join(UPLOAD_DIR, candidate)

    if os.path.dirname(os.path.abspath(destination)) != os.path.abspath(UPLOAD_DIR):
        return jsonify({"ok": False, "error": "rejected: filename escapes the upload folder"}), 400

    try:
        upload.save(destination)
    except OSError as exc:
        return jsonify({"ok": False, "error": f"could not save the upload: {exc}"}), 500

    if os.path.getsize(destination) == 0:
        os.remove(destination)
        return jsonify({"ok": False, "error": "the uploaded file was empty"}), 400

    # Prove it is really a decodable video before offering it as a source,
    # rather than letting a lane discover that when it goes blank.
    playable, why = vision.probe_video(destination)
    if not playable:
        os.remove(destination)
        return jsonify({"ok": False, "error": f"that file is not a playable video ({why})"}), 400

    result = {"ok": True, "video": candidate, "videos": vision.list_videos(), "lane": lane_name}
    if lane_name is not None:
        # assign_video releases the old capture, opens this one from frame 0
        # and clears the lane's counting state, so the lane recalibrates and
        # starts counting the new clip with no carry-over.
        assigned, message = vision_manager.assign_video(lane_name, candidate)
        result["ok"] = assigned
        result["message"] = message
        if not assigned:
            return jsonify(result), 500
    else:
        result["message"] = f"Uploaded {candidate}"
    return jsonify(result)


@app.route("/api/lane/<lane>/video", methods=["POST"])
def api_lane_video(lane):
    """{"video": "lane2.mp4"} assigns, {"video": null} or "none" detaches.

    The name is validated against the scanned catalogue of clips, so a
    client can never name a path - only an existing file in an allowed
    directory. Assigning the same clip to two lanes is permitted; each
    lane decodes its own independent capture.
    """
    lane_name, error = _lane_or_400(lane)
    if error:
        return error

    requested = _json_body().get("video")
    if isinstance(requested, str):
        requested = requested.strip()
        if requested.lower() in ("", "none", "null"):
            requested = None
    elif requested is not None:
        return jsonify({"ok": False, "error": "video must be a filename string or null"}), 400

    if requested is not None and vision.resolve_video(requested) is None:
        return jsonify({"ok": False, "lane": lane_name,
                        "error": f"'{requested}' is not one of the available videos",
                        "videos": vision.list_videos()}), 400

    ok, message = vision_manager.assign_video(lane_name, requested)
    return jsonify({"ok": ok, "lane": lane_name, "video": requested, "message": message}), (200 if ok else 500)


@app.route("/api/lane/<lane>/recalibrate", methods=["POST"])
def api_lane_recalibrate(lane):
    """Reopen the lane's clip from frame 0 and discard the smoothed count,
    so the next reading is built only from post-recalibration frames."""
    lane_name, error = _lane_or_400(lane)
    if error:
        return error
    ok, message = vision_manager.recalibrate(lane_name)
    return jsonify({"ok": ok, "lane": lane_name, "message": message}), (200 if ok else 409)


@app.route("/api/recalibrate", methods=["POST"])
def api_recalibrate_all():
    results = vision_manager.recalibrate_all()
    return jsonify({
        "ok": any(ok for _lane, ok, _msg in results),
        "results": [{"lane": lane, "ok": ok, "message": msg} for lane, ok, msg in results],
    })


# -------------------------------------------------------------- serial --
@app.route("/api/serial/ports")
def api_serial_ports():
    return jsonify({"ok": True, "ports": arduino.available_ports(), **arduino.status()})


@app.route("/api/serial/connect", methods=["POST"])
def api_serial_connect():
    ok, message = arduino.connect(_json_body().get("port"))
    if ok:
        # Push the current state immediately so the LEDs match the
        # dashboard instead of waiting for the next phase change.
        arduino.send_state(state.signal_string)
    return jsonify({"ok": ok, "message": message, **arduino.status()}), (200 if ok else 400)


@app.route("/api/serial/disconnect", methods=["POST"])
def api_serial_disconnect():
    arduino.disconnect()
    return jsonify({"ok": True, "message": "Serial link closed. Running without hardware.",
                    **arduino.status()})


# ---------------------------------------------------------- simulation --
@app.route("/api/simulate", methods=["POST"])
def api_simulate():
    """Run the offline model. Touches no live state beyond storing the
    result for display, sends nothing to the serial port, and uses the
    same traffic_logic functions as the live runner."""
    body = _json_body()
    try:
        # Bounded on purpose: duration drives a fixed-step loop, so an
        # unchecked value from the browser would pin a worker thread.
        seed = _number(body.get("seed", int(time.time()) % 100000),
                       "seed", 0, 2 ** 31 - 1, integer=True)
        duration = _number(body.get("duration_sec", 900.0), "duration_sec", 60.0, 7200.0)
        fixed_green = _number(body.get("fixed_green_sec", simulation.FIXED_TIMER_GREEN_SEC),
                              "fixed_green_sec", 1.0, MAX_GREEN_SEC)
        rates = body.get("arrival_rates")
        if rates is not None:
            if not isinstance(rates, dict):
                raise ValueError("arrival_rates must be an object of lane -> veh/min")
            rates = {resolve_lane(key): _number(value, f"arrival_rates[{key}]", 0.0, 240.0)
                     for key, value in rates.items() if resolve_lane(key) is not None}
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    try:
        result = simulation.benchmark(seed=int(seed), duration_sec=duration,
                                      arrival_rates=rates, fixed_green_sec=fixed_green)
    except Exception as exc:
        app.logger.exception("simulation failed")
        return jsonify({"ok": False, "error": f"simulation failed: {exc}"}), 500
    state.set_comparison(result)
    return jsonify({"ok": True, "comparison": result})


@app.route("/api/simulate", methods=["DELETE"])
def api_simulate_clear():
    state.set_comparison(None)
    return jsonify({"ok": True})


# ---------------------------------------------------------------- logs --
@app.route("/api/logs")
def api_logs():
    n = request.args.get("n", default=12, type=int) or 12
    n = max(1, min(200, n))
    return jsonify({"ok": True, "header": csv_logger.HEADER,
                    "recent": csv_logger.get_recent_logs(n),
                    "stats": csv_logger.get_stats()})


# --------------------------------------------------------------- boot ---
@app.errorhandler(404)
def handle_404(_error):
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": f"no such endpoint: {request.path}"}), 404
    return Response("Not found", status=404, mimetype="text/plain")


@app.errorhandler(500)
def handle_500(error):  # pragma: no cover - only on an unexpected bug
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": "internal server error"}), 500
    return Response("Internal server error", status=500, mimetype="text/plain")


def start_background_workers() -> None:
    csv_logger.ensure_log_file()
    vision_manager.start(dict(DEFAULT_LANE_VIDEOS))
    threading.Thread(target=signal_runner, name="signal-runner", daemon=True).start()


start_background_workers()

if __name__ == "__main__":
    # debug/reloader off on purpose: the reloader starts a second process,
    # which would mean two signal runners fighting over one serial port.
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
