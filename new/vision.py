"""
vision.py
---------
Per-lane video capture, vehicle detection and frame serving.

One `LaneStream` per lane owns everything about that lane's feed:
its video assignment, its VideoCapture, its latest decoded frame, its
latest cached JPEG and its own vehicle count. Lanes are fully independent -
a corrupt clip on Lane 3 cannot stop Lane 1 from streaming.

Threading model
===============
    LaneStream thread  (x4)  decode -> resize -> cache raw frame
                             -> draw overlay -> cache JPEG
    Detector thread    (x1)  round-robins the lanes, runs YOLO on the
                             cached raw frame, updates that lane's count

Detection runs in exactly one thread because a YOLO model is not
thread-safe and four concurrent inferences would serialise on the GIL
anyway. Decode stays per-lane so one slow/broken file cannot stall the
others.

Counts are published for the controller to read at ITS convenience. This
module never asks the controller to end a phase early - a vehicle count
changing is not a reason to cut a green short (that was the old
`force_recalc` bug).

Frames are encoded to JPEG at most once per decoded frame, and only while
a browser is actually asking for them; HTTP requests just hand back the
cached bytes.
"""
from __future__ import annotations

import os
import threading
import time
from collections import deque
from typing import Dict, List, Optional, Tuple

from constants import (
    COUNT_SMOOTHING,
    DETECTION_CONFIDENCE,
    DETECTION_IMGSZ,
    FRAME_HEIGHT,
    FRAME_STALE_SEC,
    FRAME_WIDTH,
    JPEG_QUALITY,
    LANES,
    TARGET_DISPLAY_FPS,
    VEHICLE_CLASS_IDS,
    VIDEO_DIRS,
    VIDEO_EXTENSIONS,
    VIEWER_IDLE_SEC,
    lane_label,
)

try:
    import cv2
    CV2_AVAILABLE = True
except Exception:  # pragma: no cover - environment dependent
    cv2 = None
    CV2_AVAILABLE = False

# Status values a lane stream can report.
ST_IDLE = "idle"            # no video assigned
ST_OPENING = "opening"
ST_STREAMING = "streaming"
ST_CALIBRATING = "calibrating"
ST_ERROR = "error"
ST_UNAVAILABLE = "unavailable"  # OpenCV itself is missing

# How long a failed lane waits before trying to reopen its file.
RETRY_INTERVAL_SEC = 5.0


# ------------------------------------------------------------- catalogue --
def _scan_videos() -> Dict[str, str]:
    """Basename -> absolute path for every clip in the allowed directories.

    This is the whitelist that video assignment validates against; a name
    that is not a key here is never opened, so no client-supplied string
    ever reaches the filesystem as a path.
    """
    found: Dict[str, str] = {}
    for directory in VIDEO_DIRS:
        try:
            entries = sorted(os.listdir(directory))
        except OSError:
            continue
        for name in entries:
            if not name.lower().endswith(VIDEO_EXTENSIONS):
                continue
            path = os.path.join(directory, name)
            if os.path.isfile(path):
                found.setdefault(name, path)
    return found


def list_videos() -> List[str]:
    return sorted(_scan_videos().keys())


def probe_video(path: str) -> Tuple[bool, str]:
    """(decodable, reason). Used to vet an upload before it is offered as a
    lane source, so a renamed .txt never becomes a permanently blank lane.

    With OpenCV absent nothing can be checked, and nothing can play either;
    the caller is told so rather than being given a false pass.
    """
    if not CV2_AVAILABLE:
        return False, "opencv-python is not installed, so the file cannot be verified"
    cap = None
    try:
        cap = cv2.VideoCapture(path)
        if not cap.isOpened():
            return False, "no decoder could open it"
        ok, frame = cap.read()
        if not ok or frame is None:
            return False, "it opened but produced no frames"
        return True, "ok"
    except Exception as exc:
        return False, f"{exc.__class__.__name__}: {exc}"
    finally:
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass


def resolve_video(name: object) -> Optional[str]:
    """Absolute path for a whitelisted clip name, else None."""
    if not isinstance(name, str):
        return None
    name = name.strip()
    if not name or name != os.path.basename(name) or name in (".", ".."):
        return None
    return _scan_videos().get(name)


# -------------------------------------------------------------- detector --
class VehicleDetector:
    """YOLOv8 vehicle detector, or a disabled stub when unavailable.

    `available` is False when ultralytics/the weights file is missing. In
    that case no count is ever published and lanes fall back to the
    operator's manual counts - the dashboard says so explicitly rather
    than showing a zero that looks like a measurement.
    """

    def __init__(self, weights: str):
        self.weights = weights
        self.available = False
        self.name = "none"
        self.status = "disabled"
        self._model = None
        self._lock = threading.Lock()

        if not CV2_AVAILABLE:
            self.status = "unavailable: opencv-python is not installed"
            return
        if not os.path.exists(weights):
            self.status = f"unavailable: {os.path.basename(weights)} not found"
            return
        try:
            from ultralytics import YOLO
        except Exception as exc:
            self.status = f"unavailable: ultralytics not installed ({exc.__class__.__name__})"
            return
        try:
            self._model = YOLO(weights)
            self.available = True
            self.name = os.path.basename(weights)
            self.status = "ready"
        except Exception as exc:
            self.status = f"error loading weights: {exc}"

    def detect(self, frame) -> Tuple[int, List[Tuple[int, int, int, int]]]:
        """(vehicle_count, boxes). Never raises - a detection failure
        reports zero and records the reason."""
        if not self.available or frame is None:
            return 0, []
        try:
            with self._lock:
                result = self._model(frame, verbose=False, imgsz=DETECTION_IMGSZ)[0]
            boxes = []
            for box in result.boxes:
                if int(box.cls[0]) in VEHICLE_CLASS_IDS and float(box.conf[0]) >= DETECTION_CONFIDENCE:
                    x1, y1, x2, y2 = (int(v) for v in box.xyxy[0])
                    boxes.append((x1, y1, x2, y2))
            self.status = "ready"
            return len(boxes), boxes
        except Exception as exc:
            self.status = f"error: {exc}"
            return 0, []


# ------------------------------------------------------------ lane stream --
class LaneStream:
    """Owns one lane's video source, frames and vehicle count."""

    def __init__(self, lane: str):
        self.lane = lane
        self.label = lane_label(lane)

        self._lock = threading.RLock()
        self._commands: deque = deque()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self._cap = None
        self._video: Optional[str] = None
        self._path: Optional[str] = None
        self._source_fps = 0.0

        self._raw_frame = None          # latest decoded BGR frame (detector input)
        self._raw_frame_at = 0.0
        self._jpeg: Optional[bytes] = None
        self._jpeg_at = 0.0
        self._frame_index = 0
        self._viewer_at = 0.0

        self._boxes: List[Tuple[int, int, int, int]] = []
        self._boxes_at = 0.0
        self._count_ema: Optional[float] = None
        self._last_detection_at: Optional[float] = None
        # Bumped every time the analysis state is thrown away (assign,
        # recalibrate, fatal decode error). Detection runs for tens of
        # milliseconds OUTSIDE any lock, so a result computed from a
        # pre-reset frame can arrive after the reset. The detector carries
        # the generation it read with, and publish_detection drops anything
        # stamped with a generation that is no longer current.
        self._generation = 0
        self._detected_index = -1  # last frame index actually detected on

        self._status = ST_UNAVAILABLE if not CV2_AVAILABLE else ST_IDLE
        self._message = "" if CV2_AVAILABLE else "opencv-python is not installed"
        self._decoded_frames = 0
        self._fps_window_start = 0.0
        self._fps = 0.0
        self._loops = 0
        self._retry_at = 0.0

    # ------------------------------------------------------- lifecycle --
    def start(self) -> None:
        if not CV2_AVAILABLE or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name=f"lane-{self.lane}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=3.0)
        with self._lock:
            self._release_locked()

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # -------------------------------------------------------- commands --
    def _submit(self, kind: str, arg=None, timeout: float = 12.0) -> Tuple[bool, str]:
        """Queue a command for the worker thread and wait for its result.

        Capture objects are only ever touched by their own worker thread;
        request threads hand work over instead of racing on `_cap`.
        """
        if not CV2_AVAILABLE:
            return False, "opencv-python is not installed - video is unavailable"
        if not self.alive:
            return False, f"{self.label} worker is not running"
        done = threading.Event()
        box: Dict[str, Tuple[bool, str]] = {}
        with self._lock:
            self._commands.append((kind, arg, done, box))
        if not done.wait(timeout):
            return False, f"{self.label} worker did not respond in {timeout:.0f}s"
        return box.get("result", (False, "no result"))

    def assign_video(self, video: Optional[str]) -> Tuple[bool, str]:
        """`video` is a whitelisted basename, or None to detach the lane."""
        return self._submit("assign", video)

    def recalibrate(self) -> Tuple[bool, str]:
        return self._submit("recalibrate")

    # ----------------------------------------------------------- output --
    def get_jpeg(self) -> Optional[bytes]:
        """Cached bytes - no encoding happens on the request thread."""
        with self._lock:
            self._viewer_at = time.time()
            return self._jpeg

    def note_viewer(self) -> None:
        with self._lock:
            self._viewer_at = time.time()

    def take_raw_frame(self):
        """(frame, generation, frame_index) for the detector.

        Returns (None, generation, index) when there is no fresh frame, or
        when this frame has already been detected on - counting the same
        frame twice would weight a stalled lane's last image far more
        heavily in the average than a healthy lane's.
        """
        with self._lock:
            if (self._raw_frame is None
                    or time.time() - self._raw_frame_at > FRAME_STALE_SEC
                    or self._frame_index == self._detected_index):
                return None, self._generation, self._frame_index
            return self._raw_frame, self._generation, self._frame_index

    def publish_detection(self, count: int, boxes, generation: Optional[int] = None,
                          frame_index: Optional[int] = None) -> bool:
        """Record one detection. Returns False if it was discarded as stale.

        `generation` is what take_raw_frame handed out. If the lane has been
        reassigned or recalibrated since, this result describes a frame from
        a video (or a playback position) the lane is no longer showing, and
        must not touch the count.
        """
        with self._lock:
            if generation is not None and generation != self._generation:
                return False
            if frame_index is not None:
                self._detected_index = frame_index
            previous = self._count_ema
            self._count_ema = float(count) if previous is None else (
                COUNT_SMOOTHING * count + (1.0 - COUNT_SMOOTHING) * previous)
            self._boxes = list(boxes)
            self._boxes_at = time.time()
            self._last_detection_at = self._boxes_at
            if self._status == ST_CALIBRATING:
                self._status = ST_STREAMING
                self._message = ""
            return True

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._cap is not None and (time.time() - self._raw_frame_at) <= FRAME_STALE_SEC

    def report(self) -> dict:
        with self._lock:
            now = time.time()
            has_frame = self._raw_frame is not None
            connected = self._cap is not None and (now - self._raw_frame_at) <= FRAME_STALE_SEC
            count = None if self._count_ema is None else max(0, int(round(self._count_ema)))
            return {
                "assigned_video": self._video,
                "connected": connected,
                "status": self._status,
                "message": self._message,
                "vision_count": count if connected else None,
                "frame_age_sec": round(now - self._raw_frame_at, 2) if has_frame else None,
                # Reports the decoder, not the JPEG cache. Deriving it from
                # the cache deadlocked the UI: the client only polls
                # /api/frame when has_frame is true, but the cache only
                # filled once a client had polled.
                "has_frame": has_frame,
                "fps": round(self._fps, 1),
                "source_fps": round(self._source_fps, 2),
                "loops": self._loops,
                "last_detection_age_sec": (round(now - self._last_detection_at, 2)
                                           if self._last_detection_at else None),
            }

    # ------------------------------------------------------ worker loop --
    def _run(self) -> None:
        while not self._stop.is_set():
            started = time.time()
            try:
                self._drain_commands()
                if self._cap is None:
                    self._maybe_retry()
                    self._stop.wait(0.15)
                    continue
                if not self._pump_frame():
                    self._stop.wait(0.25)
                    continue
                self._pace(started)
            except Exception as exc:  # a worker must never die silently
                with self._lock:
                    self._status = ST_ERROR
                    self._message = f"worker error: {exc}"
                    self._release_locked()
                    self._retry_at = time.time() + RETRY_INTERVAL_SEC
                self._stop.wait(1.0)

    def _maybe_retry(self) -> None:
        """A lane whose file went missing or whose decoder died reopens
        itself on a slow timer instead of staying dead until someone
        notices."""
        with self._lock:
            if self._status != ST_ERROR or not self._video or not self._path:
                return
            now = time.time()
            if now < self._retry_at:
                return
            self._retry_at = now + RETRY_INTERVAL_SEC
            video, path = self._video, self._path
        if os.path.exists(path):
            self._open(video, path)

    def _drain_commands(self) -> None:
        while True:
            with self._lock:
                if not self._commands:
                    return
                kind, arg, done, box = self._commands.popleft()
            try:
                if kind == "assign":
                    box["result"] = self._do_assign(arg)
                elif kind == "recalibrate":
                    box["result"] = self._do_recalibrate()
                else:
                    box["result"] = (False, f"unknown command {kind!r}")
            except Exception as exc:
                box["result"] = (False, f"{kind} failed: {exc}")
            finally:
                done.set()

    def _do_assign(self, video: Optional[str]) -> Tuple[bool, str]:
        with self._lock:
            self._release_locked()
            self._reset_analysis_locked()
            self._video = None
            self._path = None
            self._jpeg = None
            self._loops = 0     # a new clip starts its loop count over
            self._status = ST_IDLE
            self._message = ""

        if video is None:
            return True, f"{self.label}: video source detached"

        path = resolve_video(video)
        if path is None:
            with self._lock:
                self._status = ST_ERROR
                self._message = f"'{video}' is not an available video file"
            return False, f"'{video}' is not an available video file"

        ok, message = self._open(video, path)
        return ok, message

    def _open(self, video: str, path: str) -> Tuple[bool, str]:
        with self._lock:
            self._status = ST_OPENING
            self._message = ""
        cap = cv2.VideoCapture(path)
        if not cap.isOpened():
            try:
                cap.release()
            except Exception:
                pass
            with self._lock:
                self._status = ST_ERROR
                self._message = f"could not open {video} (missing or unsupported codec)"
                self._video = video
                self._path = path
            return False, f"{self.label}: could not open {video}"

        ok, frame = cap.read()
        if not ok or frame is None:
            cap.release()
            with self._lock:
                self._status = ST_ERROR
                self._message = f"{video} opened but produced no frames (corrupt file?)"
                self._video = video
                self._path = path
            return False, f"{self.label}: {video} produced no frames"

        fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        now = time.time()
        # Keep the frame we just read to prove the file decodes, instead of
        # discarding it: it makes the lane displayable the moment this call
        # returns, with no blank gap while the worker reaches its next loop.
        first = cv2.resize(frame, (FRAME_WIDTH, FRAME_HEIGHT))
        with self._lock:
            self._cap = cap
            self._video = video
            self._path = path
            self._source_fps = fps if fps and fps == fps and fps < 240 else 0.0
            self._status = ST_CALIBRATING
            self._message = ""
            self._fps_window_start = now
            self._decoded_frames = 0
            self._raw_frame = first
            self._raw_frame_at = now
        self._encode(first, [], None, now)
        return True, f"{self.label}: now showing {video}"

    def _do_recalibrate(self) -> Tuple[bool, str]:
        with self._lock:
            video, path = self._video, self._path
            self._reset_analysis_locked()
        if video is None or path is None:
            return False, f"{self.label}: nothing to recalibrate - no video assigned"
        with self._lock:
            self._release_locked()
        ok, message = self._open(video, path)
        if not ok:
            return False, message
        return True, f"{self.label}: recalibrated against {video}"

    def _reset_analysis_locked(self) -> None:
        """Throw away everything derived from the old frames.

        The raw frame goes too, not just the count: leaving it behind lets
        the detector pick up a pre-recalibration image on its very next
        pass, which is exactly how the first post-recalibration count used
        to end up describing the old footage.
        """
        self._generation += 1
        self._count_ema = None
        self._boxes = []
        self._boxes_at = 0.0
        self._last_detection_at = None
        self._raw_frame = None
        self._raw_frame_at = 0.0
        self._detected_index = -1
        self._frame_index = 0

    def _release_locked(self) -> None:
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:
                pass
        self._cap = None

    def _pump_frame(self) -> bool:
        """Decode one display frame. Returns False when the lane is broken
        and the caller should back off."""
        cap = self._cap
        skip = 1
        if self._source_fps > TARGET_DISPLAY_FPS:
            skip = max(1, int(round(self._source_fps / TARGET_DISPLAY_FPS)))
        for _ in range(skip - 1):
            if not cap.grab():
                break

        ok, frame = cap.read()
        if not ok or frame is None:
            if not self._restart_clip():
                return False
            cap = self._cap
            ok, frame = cap.read()
            if not ok or frame is None:
                with self._lock:
                    self._status = ST_ERROR
                    self._message = "video ended and could not be restarted"
                    self._release_locked()
                return False

        frame = cv2.resize(frame, (FRAME_WIDTH, FRAME_HEIGHT))
        now = time.time()
        with self._lock:
            self._raw_frame = frame
            self._raw_frame_at = now
            self._frame_index += 1
            self._decoded_frames += 1
            if self._status in (ST_OPENING, ST_ERROR):
                self._status = ST_CALIBRATING
                self._message = ""
            elapsed = now - self._fps_window_start
            if elapsed >= 2.0:
                self._fps = self._decoded_frames / elapsed
                self._decoded_frames = 0
                self._fps_window_start = now
            # Always prime an empty cache so the very first poll gets a real
            # image instead of a 404; after that, only keep encoding while
            # somebody is actually watching.
            wants_jpeg = self._jpeg is None or (now - self._viewer_at) <= VIEWER_IDLE_SEC
            boxes = list(self._boxes) if (now - self._boxes_at) <= 1.0 else []
            # Same expression report() publishes, so the number burnt into
            # the frame can never disagree with the one in the dashboard.
            count = None if self._count_ema is None else max(0, int(round(self._count_ema)))

        if wants_jpeg:
            self._encode(frame, boxes, count, now)
        return True

    def _restart_clip(self) -> bool:
        """End of file: rewind. If seeking fails (some codecs/streams),
        reopen the file from scratch."""
        cap = self._cap
        try:
            if cap is not None and cap.set(cv2.CAP_PROP_POS_FRAMES, 0):
                ok, _ = cap.read()
                if ok:
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    with self._lock:
                        self._loops += 1
                    return True
        except Exception:
            pass

        with self._lock:
            video, path = self._video, self._path
            self._release_locked()
        if not video or not path or not os.path.exists(path):
            with self._lock:
                self._status = ST_ERROR
                self._message = "video file is no longer readable"
            return False
        ok, _message = self._open(video, path)
        if ok:
            with self._lock:
                self._loops += 1
        return ok

    def _encode(self, frame, boxes, count, now: float) -> None:
        """Annotate and JPEG-encode once per decoded frame, off the request
        path. Boxes older than a second are dropped by the caller so the
        overlay never shows detections from a frame long gone."""
        canvas = frame.copy()
        for (x1, y1, x2, y2) in boxes:
            cv2.rectangle(canvas, (x1, y1), (x2, y2), (95, 229, 67), 1)
        badge = f"{self.label} · {self.lane}"
        if count is not None:
            badge += f" · {count} vehicles"
        cv2.rectangle(canvas, (0, FRAME_HEIGHT - 26), (FRAME_WIDTH, FRAME_HEIGHT), (12, 12, 12), -1)
        cv2.putText(canvas, badge, (10, FRAME_HEIGHT - 8),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (238, 243, 246), 1, cv2.LINE_AA)
        ok, buffer = cv2.imencode(".jpg", canvas, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY])
        if ok:
            data = buffer.tobytes()
            with self._lock:
                self._jpeg = data
                self._jpeg_at = now

    def _pace(self, loop_started: float) -> None:
        """Hold the display rate near TARGET_DISPLAY_FPS. If decoding is
        slower than that (4K source on a busy CPU) this simply never
        sleeps and the lane runs as fast as it can."""
        remaining = (1.0 / TARGET_DISPLAY_FPS) - (time.time() - loop_started)
        if remaining > 0:
            self._stop.wait(remaining)


# ----------------------------------------------------------- the manager --
class VisionManager:
    """Owns the four lane streams and the single detector thread."""

    def __init__(self, weights: str, lanes=LANES):
        self.lanes = list(lanes)
        self.weights = weights
        # Built on the detector thread: loading YOLO takes seconds and must
        # not delay the web server coming up.
        self.detector: Optional[VehicleDetector] = None
        self.streams: Dict[str, LaneStream] = {lane: LaneStream(lane) for lane in self.lanes}
        self._stop = threading.Event()
        self._detector_thread: Optional[threading.Thread] = None
        self._detector_error: Optional[str] = None

    # ------------------------------------------------------- lifecycle --
    def start(self, initial_videos: Dict[str, Optional[str]]) -> None:
        for stream in self.streams.values():
            stream.start()
        for lane, video in initial_videos.items():
            stream = self.streams.get(lane)
            if stream is None:
                continue
            if video is not None and resolve_video(video) is None:
                video = None
            stream.assign_video(video)
        self._detector_thread = threading.Thread(target=self._detect_loop,
                                                 name="detector", daemon=True)
        self._detector_thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._detector_thread is not None:
            self._detector_thread.join(timeout=3.0)
        for stream in self.streams.values():
            stream.stop()

    # ---------------------------------------------------------- queries --
    def report(self) -> Dict[str, dict]:
        return {lane: stream.report() for lane, stream in self.streams.items()}

    def counts(self) -> Dict[str, Optional[int]]:
        return {lane: stream.report()["vision_count"] for lane, stream in self.streams.items()}

    def frame(self, lane: str) -> Optional[bytes]:
        stream = self.streams.get(lane)
        return stream.get_jpeg() if stream else None

    def status(self) -> dict:
        detector = self.detector
        return {
            "opencv_available": CV2_AVAILABLE,
            "detector_available": bool(detector and detector.available),
            "detector": detector.name if detector else "loading",
            "detector_status": (self._detector_error or (detector.status if detector
                                else "loading detector...")),
            "workers_alive": sum(1 for s in self.streams.values() if s.alive),
            "workers_expected": len(self.streams),
        }

    # --------------------------------------------------------- commands --
    def assign_video(self, lane: str, video: Optional[str]) -> Tuple[bool, str]:
        stream = self.streams.get(lane)
        if stream is None:
            return False, f"unknown lane {lane!r}"
        return stream.assign_video(video)

    def recalibrate(self, lane: str) -> Tuple[bool, str]:
        stream = self.streams.get(lane)
        if stream is None:
            return False, f"unknown lane {lane!r}"
        return stream.recalibrate()

    def recalibrate_all(self) -> List[Tuple[str, bool, str]]:
        return [(lane, *self.streams[lane].recalibrate()) for lane in self.lanes]

    # ---------------------------------------------------- detector loop --
    def _detect_loop(self) -> None:
        self.detector = VehicleDetector(self.weights)
        if not self.detector.available:
            # No detector: lanes still stream video, counts stay manual and
            # the dashboard says why instead of showing a fake zero.
            return

        index = 0
        while not self._stop.is_set():
            lane = self.lanes[index % len(self.lanes)]
            index += 1
            stream = self.streams[lane]
            try:
                # The generation is read WITH the frame and handed back with
                # the result. Inference happens outside the lane's lock, so
                # the lane may be reassigned or recalibrated meanwhile; the
                # stamp is what lets the lane reject the obsolete answer
                # instead of adopting it as its first new count.
                frame, generation, frame_index = stream.take_raw_frame()
                if frame is None:
                    self._stop.wait(0.05)
                    continue
                count, boxes = self.detector.detect(frame)
                stream.publish_detection(count, boxes, generation, frame_index)
                self._detector_error = None
            except Exception as exc:
                self._detector_error = f"detector error: {exc}"
                self._stop.wait(1.0)
