import os
import sys
import time
from collections import defaultdict, deque

# ============================================================
# WINDOWS PYTORCH DLL FIX
# ============================================================

if sys.platform == "win32":
    torch_lib = os.path.join(
        sys.prefix,
        "Lib",
        "site-packages",
        "torch",
        "lib"
    )

    if os.path.exists(torch_lib):
        os.add_dll_directory(torch_lib)


# ============================================================
# IMPORTS
# ============================================================

import cv2
import numpy as np
from ultralytics import YOLO


# ============================================================
# YOLO MODEL
# ============================================================

model = YOLO("yolov8n.pt")

# COCO vehicle classes
# 2 = car
# 3 = motorcycle
# 5 = bus
# 7 = truck

VEHICLE_CLASS_IDS = [2, 3, 5, 7]

CONFIDENCE_THRESHOLD = 0.4


# ============================================================
# LANE VIDEOS
# ============================================================

LANE_VIDEOS = {
    "Frame 1": "lane1.mp4",
    "Frame 2": "lane2.mp4",
    "Frame 3": "lane3.mp4",
    "Frame 4": "lane4.mp4"
}

LANES = list(LANE_VIDEOS.keys())


caps = {
    lane: cv2.VideoCapture(path)
    for lane, path in LANE_VIDEOS.items()
}


# ============================================================
# TRAFFIC SETTINGS
# ============================================================

MIN_GREEN = 20

NORMAL_MAX_GREEN = 60

EMERGENCY_MAX_GREEN = 120

YELLOW_TIME = 3


# Emergency mode is cleared after this much time
# without detecting the emergency flashing pattern.

EMERGENCY_CLEAR_TIMEOUT = 3.0


# How often traffic priority is recalculated.

CONTROL_UPDATE_INTERVAL = 1.0


# ============================================================
# VEHICLE COUNTS
# ============================================================

final_counts = {
    lane: 0
    for lane in LANES
}


# ============================================================
# EMERGENCY LIGHT HISTORY
# ============================================================

# Each tracked vehicle gets a small history of
# RED / BLUE / BOTH / NONE observations.

light_history = defaultdict(
    lambda: deque(maxlen=12)
)


# ============================================================
# DETECT RED AND BLUE LIGHTS
# ============================================================

def detect_emergency_lights(frame, box):

    x1, y1, x2, y2 = box

    height, width = frame.shape[:2]

    # Keep bounding box inside frame

    x1 = max(0, x1)
    y1 = max(0, y1)

    x2 = min(width, x2)
    y2 = min(height, y2)

    if x2 <= x1 or y2 <= y1:
        return False, False


    vehicle = frame[y1:y2, x1:x2]


    if vehicle.size == 0:
        return False, False


    # Look mainly at the upper part of the vehicle.

    upper_height = max(
        1,
        int(vehicle.shape[0] * 0.45)
    )

    vehicle = vehicle[:upper_height]


    # Convert BGR to HSV.

    hsv = cv2.cvtColor(
        vehicle,
        cv2.COLOR_BGR2HSV
    )


    # ========================================================
    # RED DETECTION
    # ========================================================

    red_lower_1 = np.array(
        [0, 100, 100]
    )

    red_upper_1 = np.array(
        [10, 255, 255]
    )


    red_lower_2 = np.array(
        [170, 100, 100]
    )

    red_upper_2 = np.array(
        [180, 255, 255]
    )


    red_mask_1 = cv2.inRange(
        hsv,
        red_lower_1,
        red_upper_1
    )


    red_mask_2 = cv2.inRange(
        hsv,
        red_lower_2,
        red_upper_2
    )


    red_mask = (
        red_mask_1 |
        red_mask_2
    )


    # ========================================================
    # BLUE DETECTION
    # ========================================================

    blue_lower = np.array(
        [90, 100, 100]
    )

    blue_upper = np.array(
        [140, 255, 255]
    )


    blue_mask = cv2.inRange(
        hsv,
        blue_lower,
        blue_upper
    )


    # ========================================================
    # CALCULATE COLOR RATIOS
    # ========================================================

    total_pixels = (
        vehicle.shape[0] *
        vehicle.shape[1]
    )


    if total_pixels == 0:
        return False, False


    red_ratio = (
        cv2.countNonZero(red_mask)
        / total_pixels
    )


    blue_ratio = (
        cv2.countNonZero(blue_mask)
        / total_pixels
    )


    # Thresholds for prototype detection.

    red_detected = red_ratio > 0.015

    blue_detected = blue_ratio > 0.015


    return red_detected, blue_detected


# ============================================================
# CHECK FOR FLASHING RED/BLUE PATTERN
# ============================================================

def update_emergency_history(
    track_id,
    red,
    blue
):

    if track_id is None:
        return False


    # Determine current observation.

    if red and not blue:

        state = "RED"

    elif blue and not red:

        state = "BLUE"

    elif red and blue:

        state = "BOTH"

    else:

        state = "NONE"


    light_history[track_id].append(
        state
    )


    history = list(
        light_history[track_id]
    )


    # Need evidence of both colors.

    red_count = history.count(
        "RED"
    )

    blue_count = history.count(
        "BLUE"
    )


    if red_count < 2 or blue_count < 2:
        return False


    # Ignore NONE and BOTH when checking
    # the alternating red/blue pattern.

    meaningful = [
        state
        for state in history
        if state == "RED"
        or state == "BLUE"
    ]


    if len(meaningful) < 4:
        return False


    # Count red -> blue / blue -> red transitions.

    transitions = 0


    for i in range(
        1,
        len(meaningful)
    ):

        if (
            meaningful[i]
            != meaningful[i - 1]
        ):

            transitions += 1


    # Multiple transitions indicate flashing.

    return transitions >= 2


# ============================================================
# TRAFFIC SIGNAL CONTROLLER
# ============================================================

class TrafficSignalController:

    def __init__(
        self,
        lanes,
        min_green=20,
        normal_max_green=60,
        emergency_max_green=120,
        yellow_time=3
    ):

        self.lanes = lanes

        self.min_green = min_green

        self.normal_max_green = (
            normal_max_green
        )

        self.emergency_max_green = (
            emergency_max_green
        )

        self.yellow_time = yellow_time


    # ========================================================
    # SIGNAL STRING
    # ========================================================

    def get_signal_state_string(
        self,
        active_lane,
        color
    ):

        return "".join(
            color
            if lane == active_lane
            else "R"
            for lane in self.lanes
        )


    # ========================================================
    # CALCULATE GREEN TIMES
    # ========================================================

    def calculate_durations(
        self,
        lane_counts,
        emergency=False
    ):

        total_vehicles = sum(
            lane_counts.values()
        )


        # Select normal or emergency maximum.

        if emergency:

            max_green = (
                self.emergency_max_green
            )

        else:

            max_green = (
                self.normal_max_green
            )


        # No vehicles.

        if total_vehicles == 0:

            return {
                lane: self.min_green
                for lane in self.lanes
            }


        durations = {}


        for lane in self.lanes:

            count = lane_counts.get(
                lane,
                0
            )


            # Density ratio.

            ratio = (
                count /
                total_vehicles
            )


            # SAME FORMULA IN BOTH MODES

            green_time = (
                self.min_green
                +
                (
                    max_green -
                    self.min_green
                )
                * ratio
            )


            durations[lane] = round(
                green_time
            )


        return durations


    # ========================================================
    # NORMAL PRIORITY
    # ========================================================

    def get_normal_priority(
        self,
        lane_counts
    ):

        # Highest current vehicle count first.

        return sorted(
            self.lanes,
            key=lambda lane:
                lane_counts.get(
                    lane,
                    0
                ),
            reverse=True
        )


    # ========================================================
    # PRIORITY WITH EMERGENCY
    # ========================================================

    def get_priority(
        self,
        lane_counts,
        emergency_lane=None
    ):

        normal_order = (
            self.get_normal_priority(
                lane_counts
            )
        )


        # No emergency.

        if emergency_lane is None:

            return normal_order


        # Emergency lane goes first.

        priority = [
            emergency_lane
        ]


        # Keep normal density order
        # for all other lanes.

        for lane in normal_order:

            if lane != emergency_lane:

                priority.append(
                    lane
                )


        return priority


    # ========================================================
    # CREATE SIGNAL SEQUENCE
    # ========================================================

    def get_execution_sequence(
        self,
        lane_counts,
        emergency_lane=None
    ):

        emergency = (
            emergency_lane is not None
        )


        durations = (
            self.calculate_durations(
                lane_counts,
                emergency
            )
        )


        priority_order = (
            self.get_priority(
                lane_counts,
                emergency_lane
            )
        )


        sequence = []


        for lane in priority_order:

            # ------------------------------------------------
            # GREEN
            # ------------------------------------------------

            sequence.append({

                "lane": lane,

                "signal":
                    self.get_signal_state_string(
                        lane,
                        "G"
                    ),

                "duration":
                    durations[lane],

                "status":
                    f"{lane} GREEN",

                "emergency":
                    lane == emergency_lane
            })


            # ------------------------------------------------
            # YELLOW
            # ------------------------------------------------

            sequence.append({

                "lane": lane,

                "signal":
                    self.get_signal_state_string(
                        lane,
                        "Y"
                    ),

                "duration":
                    self.yellow_time,

                "status":
                    f"{lane} YELLOW",

                "emergency":
                    lane == emergency_lane
            })


        return sequence


# ============================================================
# INITIALIZE CONTROLLER
# ============================================================

controller = TrafficSignalController(

    lanes=LANES,

    min_green=MIN_GREEN,

    normal_max_green=
        NORMAL_MAX_GREEN,

    emergency_max_green=
        EMERGENCY_MAX_GREEN,

    yellow_time=YELLOW_TIME
)


# ============================================================
# SIGNAL STATE
# ============================================================

current_sequence = []

sequence_index = 0

phase_started_at = time.time()

last_control_update = 0


# ============================================================
# EMERGENCY STATE
# ============================================================

emergency_lane = None

last_emergency_detection = 0


# ============================================================
# START SIGNAL PHASE
# ============================================================

def start_phase(index):

    global sequence_index
    global phase_started_at


    if not current_sequence:
        return


    sequence_index = (
        index %
        len(current_sequence)
    )


    phase_started_at = time.time()


# ============================================================
# MAIN LOOP
# ============================================================

while True:

    frames = {}


    # ========================================================
    # PROCESS ALL FOUR LANES
    # ========================================================

    for lane, cap in caps.items():

        ret, frame = cap.read()


        # Restart video when it ends.

        if not ret:

            cap.set(
                cv2.CAP_PROP_POS_FRAMES,
                0
            )

            ret, frame = cap.read()


        if not ret:
            continue


        # Resize all feeds.

        frame = cv2.resize(
            frame,
            (640, 360)
        )


        # ====================================================
        # YOLO TRACKING
        # ====================================================

        results = model.track(

            frame,

            persist=True,

            verbose=False,

            tracker="bytetrack.yaml"
        )[0]


        count = 0

        emergency_detected_in_lane = False


        # ====================================================
        # PROCESS DETECTED VEHICLES
        # ====================================================

        if results.boxes is not None:

            for box in results.boxes:

                class_id = int(
                    box.cls[0]
                )

                confidence = float(
                    box.conf[0]
                )


                # Only process vehicles.

                if (
                    class_id
                    not in VEHICLE_CLASS_IDS
                    or
                    confidence
                    <= CONFIDENCE_THRESHOLD
                ):

                    continue


                count += 1


                x1, y1, x2, y2 = map(
                    int,
                    box.xyxy[0]
                )


                # =================================================
                # EMERGENCY LIGHT DETECTION
                # =================================================

                red, blue = (
                    detect_emergency_lights(
                        frame,
                        (
                            x1,
                            y1,
                            x2,
                            y2
                        )
                    )
                )


                # Get YOLO tracking ID.

                track_id = None


                if box.id is not None:

                    track_id = int(
                        box.id[0]
                    )


                possible_emergency = (
                    update_emergency_history(
                        track_id,
                        red,
                        blue
                    )
                )


                # =================================================
                # DRAW DETECTION
                # =================================================

                if possible_emergency:

                    emergency_detected_in_lane = True


                    # Red box for emergency vehicle.

                    cv2.rectangle(

                        frame,

                        (x1, y1),

                        (x2, y2),

                        (0, 0, 255),

                        3
                    )


                    cv2.putText(

                        frame,

                        "EMERGENCY",

                        (
                            x1,
                            max(
                                20,
                                y1 - 10
                            )
                        ),

                        cv2.FONT_HERSHEY_SIMPLEX,

                        0.7,

                        (0, 0, 255),

                        2
                    )


                else:

                    # Normal vehicle.

                    cv2.rectangle(

                        frame,

                        (x1, y1),

                        (x2, y2),

                        (0, 255, 0),

                        2
                    )


        # ====================================================
        # UPDATE VEHICLE COUNT
        # ====================================================

        final_counts[lane] = count


        # ====================================================
        # EMERGENCY DETECTED
        # ====================================================

        if emergency_detected_in_lane:

            emergency_lane = lane

            last_emergency_detection = (
                time.time()
            )


        # ====================================================
        # DISPLAY COUNT
        # ====================================================

        cv2.putText(

            frame,

            f"{lane}: {count} Vehicles",

            (15, 35),

            cv2.FONT_HERSHEY_SIMPLEX,

            0.8,

            (0, 255, 255),

            2
        )


        # ====================================================
        # DISPLAY EMERGENCY STATUS
        # ====================================================

        if emergency_lane == lane:

            cv2.putText(

                frame,

                "EMERGENCY PRIORITY",

                (15, 65),

                cv2.FONT_HERSHEY_SIMPLEX,

                0.65,

                (0, 0, 255),

                2
            )


        frames[lane] = frame


    # ========================================================
    # CHECK WHETHER EMERGENCY HAS STOPPED
    # ========================================================

    if emergency_lane is not None:

        time_since_detection = (
            time.time()
            -
            last_emergency_detection
        )


        # If no emergency flashing has been
        # detected for 3 seconds, clear it.

        if (
            time_since_detection
            >= EMERGENCY_CLEAR_TIMEOUT
        ):

            print(
                f"\nEmergency cleared from "
                f"{emergency_lane}."
            )

            print(
                "Returning to normal "
                "density-based priority."
            )


            emergency_lane = None


            # Force the controller to build
            # a completely fresh normal sequence.

            current_sequence = []

            sequence_index = 0


    # ========================================================
    # UPDATE TRAFFIC CONTROL
    # ========================================================

    now = time.time()


    if (
        now -
        last_control_update
        >= CONTROL_UPDATE_INTERVAL
    ):

        last_control_update = now


        # Build a sequence using CURRENT counts.

        new_sequence = (
            controller.get_execution_sequence(

                final_counts,

                emergency_lane
            )
        )


        # If there is no active sequence,
        # start a new one.

        if not current_sequence:

            current_sequence = (
                new_sequence
            )

            start_phase(0)


        else:

            current_step = (
                current_sequence[
                    sequence_index
                ]
            )


            elapsed = (
                now -
                phase_started_at
            )


            # Move to next phase when
            # current phase expires.

            if (
                elapsed
                >=
                current_step[
                    "duration"
                ]
            ):

                next_index = (
                    sequence_index + 1
                ) % len(
                    current_sequence
                )


                current_sequence = (
                    new_sequence
                )


                # Start at the corresponding
                # position in the newly calculated
                # priority sequence.

                start_phase(
                    next_index
                )


    # ========================================================
    # CURRENT SIGNAL
    # ========================================================

    if current_sequence:

        current_step = (
            current_sequence[
                sequence_index
            ]
        )


        elapsed = (
            time.time()
            -
            phase_started_at
        )


        remaining = max(

            0,

            int(
                current_step[
                    "duration"
                ]
                -
                elapsed
            )
        )


        signal_text = (
            current_step[
                "signal"
            ]
        )


        if current_step[
            "emergency"
        ]:

            mode_text = (
                "EMERGENCY PRIORITY"
            )

        else:

            mode_text = (
                "NORMAL OPERATION"
            )


    else:

        signal_text = "RRRR"

        remaining = 0

        mode_text = "NO TRAFFIC"


    # ========================================================
    # 2 x 2 VIDEO GRID
    # ========================================================

    blank = np.zeros(
        (360, 640, 3),
        dtype=np.uint8
    )


    frame1 = frames.get(
        "Frame 1",
        blank
    )

    frame2 = frames.get(
        "Frame 2",
        blank
    )

    frame3 = frames.get(
        "Frame 3",
        blank
    )

    frame4 = frames.get(
        "Frame 4",
        blank
    )


    top_row = np.hstack(
        (
            frame1,
            frame2
        )
    )


    bottom_row = np.hstack(
        (
            frame3,
            frame4
        )
    )


    grid = np.vstack(
        (
            top_row,
            bottom_row
        )
    )


    # ========================================================
    # DISPLAY SIGNAL INFORMATION
    # ========================================================

    cv2.putText(

        grid,

        f"SIGNAL: {signal_text}",

        (20, 735),

        cv2.FONT_HERSHEY_SIMPLEX,

        0.9,

        (255, 255, 255),

        2
    )


    cv2.putText(

        grid,

        f"TIME REMAINING: {remaining}s",

        (20, 770),

        cv2.FONT_HERSHEY_SIMPLEX,

        0.8,

        (255, 255, 255),

        2
    )


    # Red = emergency
    # Green = normal

    status_color = (

        (0, 0, 255)

        if emergency_lane is not None

        else

        (0, 255, 0)
    )


    cv2.putText(

        grid,

        mode_text,

        (20, 805),

        cv2.FONT_HERSHEY_SIMPLEX,

        0.8,

        status_color,

        2
    )


    if emergency_lane is not None:

        cv2.putText(

            grid,

            f"EMERGENCY LANE: "
            f"{emergency_lane}",

            (20, 840),

            cv2.FONT_HERSHEY_SIMPLEX,

            0.8,

            (0, 0, 255),

            2
        )


    # ========================================================
    # SHOW WINDOW
    # ========================================================

    cv2.imshow(

        "Smart Traffic Control System",

        grid
    )


    # ========================================================
    # QUIT
    # ========================================================

    if (
        cv2.waitKey(1)
        & 0xFF
        == ord("q")
    ):

        break


# ============================================================
# CLEANUP
# ============================================================

for cap in caps.values():

    cap.release()


cv2.destroyAllWindows()


# ============================================================
# FINAL COUNTS
# ============================================================

print(
    "\n" +
    "=" * 40
)

print(
    "FINAL LANE VEHICLE COUNTS"
)

print(
    "=" * 40
)


for lane, count in final_counts.items():

    print(
        f"{lane}: {count} vehicles"
    )


print(
    "\nSystem terminated."
)