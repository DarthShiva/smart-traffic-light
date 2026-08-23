def calculate_green_time(vehicle_count):

    green_time = 10 + vehicle_count * 0.5

    if green_time > 60:
        green_time = 60

    return green_time


def set_green_lane(lane):

    lights = ["RED", "RED", "RED", "RED"]

    lights[lane] = "GREEN"

    return lights