import time

from traffic_controller import calculate_green_time, set_green_lane
from vehicle_detection import get_vehicle_counts


priority = 0

while True:

    # Get the latest vehicle counts
    l1, l2, l3, l4 = get_vehicle_counts()

    vehicles = [l1, l2, l3, l4]

    # Select the lane according to priority rotation
    lane = priority

    # Calculate green time for the selected lane
    green_time = calculate_green_time(vehicles[lane])

    # Set selected lane to GREEN
    lights = set_green_lane(lane)

    print("Lane:", lane + 1)
    print("Lights:", lights)
    print("Green time:", green_time, "seconds")

    # Keep the selected lane green
    elapsed_time = 0

    while elapsed_time < green_time:

        # Wait for 1 second
        time.sleep(1)

        elapsed_time += 1

        # Get updated traffic information
        l1, l2, l3, l4 = get_vehicle_counts()

        vehicles = [l1, l2, l3, l4]

    # Move priority to the next lane
    priority = (priority + 1) % 4