import os
import sys

# Register PyTorch binary path for Windows DLL resolution
if sys.platform == "win32":
    torch_lib = os.path.join(sys.prefix, "Lib", "site-packages", "torch", "lib")
    if os.path.exists(torch_lib):
        os.add_dll_directory(torch_lib)

import cv2
import numpy as np
from ultralytics import YOLO

model = YOLO("yolov8n.pt")
VEHICLE_CLASS_IDS = [2, 3, 5, 7]

LANE_VIDEOS = {
    "Frame 1": "lane1.mp4",
    "Frame 2": "lane2.mp4",
    "Frame 3": "lane3.mp4",
    "Frame 4": "lane4.mp4"
}

caps = {lane: cv2.VideoCapture(path) for lane, path in LANE_VIDEOS.items()}

# Stores the latest vehicle counts for all lanes
final_counts = {"Frame 1": 0, "Frame 2": 0, "Frame 3": 0, "Frame 4": 0}

while True:
    frames = {}

    for lane, cap in caps.items():
        ret, frame = cap.read()

        if not ret:
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ret, frame = cap.read()

        frame = cv2.resize(frame, (640, 360))
        results = model(frame, verbose=False)[0]
        count = 0

        for box in results.boxes:
            class_id = int(box.cls[0])
            confidence = float(box.conf[0])

            if class_id in VEHICLE_CLASS_IDS and confidence > 0.4:
                count += 1
                x1, y1, x2, y2 = map(int, box.xyxy[0])
                cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)

        cv2.putText(frame, f"{lane}: {count} Cars", (15, 35),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 255), 2)

        frames[lane] = frame
        final_counts[lane] = count  # Updates active vehicle count per lane

    # 2x2 Grid Display
    top_row = np.hstack((frames["Frame 1"], frames["Frame 2"]))
    bottom_row = np.hstack((frames["Frame 3"], frames["Frame 4"]))
    grid = np.vstack((top_row, bottom_row))

    cv2.imshow("Smart Traffic Control System - 4 Lane Feeds", grid)

    if cv2.waitKey(1) & 0xFF == ord('q'):#this is where the terminator is
        break

for cap in caps.values():
    cap.release()
cv2.destroyAllWindows()

# --- FINAL OUTPUT FOR TRAFFIC LOGIC ---
print("\n" + "="*30)
print("FINAL LANE VEHICLE COUNTS:")
print("="*30)
for lane, count in final_counts.items():
    a=print(f"{lane}: {count} vehicles")
l1=final_counts["Frame 1"]
l2=final_counts["Frame 2"]
l3=final_counts["Frame 3"]
l4=final_counts["Frame 4"]
# Example usage for traffic signal decision engine:
# max_lane = max(final_counts, key=final_counts.get)
# print(f"\nPriority Lane for Green Light: {max_lane}")
import time

class TrafficSignalController:
    def __init__(self, lanes=['Frame 1', 'Frame 2', 'Frame 3', 'Frame 4'], min_green=20, max_green=60, yellow_time=3):
        self.lanes = lanes
        self.min_green = min_green  # Enforced minimum 20 seconds
        self.max_green = max_green
        self.yellow_time = yellow_time

    def get_signal_state_string(self, active_lane: str, color: str) -> str:
        """
        Generates state string like 'GRRR', 'RGRR', etc.
        Order matches self.lanes: [l1, l2, l3, l4]
        """
        return "".join(color if lane == active_lane else 'R' for lane in self.lanes)

    def calculate_durations(self, lane_counts: dict) -> dict:
        """Calculates green times starting at a minimum of 20 seconds."""
        total_vehicles = sum(lane_counts.values())
        if total_vehicles == 0:
            return {lane: self.min_green for lane in self.lanes}

        durations = {}
        for lane in self.lanes:
            count = lane_counts.get(lane, 0)
            ratio = count / total_vehicles
            # Base 20 seconds + proportional share of remaining max window
            green_time = self.min_green + (self.max_green - self.min_green) * ratio
            durations[lane] = round(green_time)
        return durations

    def get_execution_sequence(self, lane_counts: dict) -> list:
        durations = self.calculate_durations(lane_counts)
        
        # Sort lanes by highest vehicle count first
        sorted_lanes = sorted(self.lanes, key=lambda l: lane_counts.get(l, 0), reverse=True)
        
        sequence = []
        for lane in sorted_lanes:
            # Green Phase
            sequence.append({
                'lane': lane,
                'signal': self.get_signal_state_string(lane, 'G'),
                'duration': durations[lane],
                'status': f"lane {lane} GREEN"
            })
            
            # Yellow Phase
            sequence.append({
                'lane': lane,
                'signal': self.get_signal_state_string(lane, 'Y'),
                'duration': self.yellow_time,
                'status': f"lane {lane} YELLOW"
            })

        return sequence
    


# --- Example Usage ---
if __name__ == "__main__":
    controller = TrafficSignalController(min_green=20, max_green=60)

    # Current snapshot of vehicle counts
    counts = {'Frame 1': l1, 'Frame 2': l2, 'Frame 3': l3, 'Frame 4': l4}

    sequence = controller.get_execution_sequence(counts)

    print("--- Signal Sequence (Min 20s Green) ---")
    for step in sequence:
        print(f"Signal: {step['signal']} | Duration: {step['duration']}s | ({step['status']})")