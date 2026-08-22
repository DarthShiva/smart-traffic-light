# smart-traffic-light
# Smart Traffic Light System

## Project Overview

Smart Traffic Light System is an intelligent traffic management system that uses real time vehicle detection to monitor traffic density across multiple lanes.

The system uses computer vision and YOLO based object detection to detect vehicles from traffic camera feeds. The detected vehicle count is used to dynamically calculate traffic signal timings.

The system also supports emergency vehicle priority and remote access for monitoring and controlling the traffic signals.

## Features

Real time vehicle detection

Four lane traffic monitoring

Dynamic green signal timing based on vehicle density

Emergency vehicle priority

Automatic traffic signal sequencing

Remote access and monitoring

Multiple traffic camera feeds

YOLO based vehicle detection

OpenCV based video processing

## Technologies Used

Python

OpenCV

YOLO

Ultralytics

NumPy

Git and GitHub

## System Workflow

Traffic camera feeds are provided for four lanes.

OpenCV reads the video feeds and processes the frames.

YOLO detects vehicles in each frame.

The number of detected vehicles is calculated for every lane.

The traffic controller compares the vehicle density between lanes.

Green signal duration is dynamically calculated.

The lane with higher traffic receives higher priority.

Emergency vehicles can receive priority over normal traffic.

The system continuously updates the traffic signal sequence.

## Traffic Signal Logic

The system maintains four traffic lanes.

Each lane receives a green duration based on its vehicle count.

The minimum green duration is 20 seconds.

The maximum green duration is 60 seconds.

A yellow phase is used between signal changes.

The lane priority is determined using the detected traffic density.

## Vehicle Detection

The system uses YOLO for detecting vehicles in the camera feeds.

The vehicle classes considered by the system include cars, motorcycles, buses and trucks.

Only detections above the specified confidence threshold are counted.

The detected vehicles are highlighted using bounding boxes.

## Emergency Vehicle Priority

Emergency vehicles can be given higher priority than normal traffic.

When an emergency vehicle is detected, the traffic controller can prioritize the corresponding lane and provide a green signal.

This helps reduce delays for emergency services.

## Remote Access

The system is designed to support remote monitoring and control.

Traffic information and signal states can be accessed remotely through the system interface.

This allows traffic conditions to be monitored without being physically present at the intersection.

## Project Structure

smart-traffic-light

main.py

traffic_controller.py

vehicle_detection.py

lane1.mp4

lane2.mp4

lane3.mp4

lane4.mp4

README.md

## How to Run

Install Python and the required dependencies.

Install the required Python packages using:

pip install opencv-python numpy ultralytics

Make sure the required YOLO model is available.

Place the lane video files in the project directory.

Run the main program using:

python main.py

Press Q to stop the video processing.

## Future Improvements

Emergency vehicle detection can be improved using a dedicated trained model.

A web based dashboard can be added for remote monitoring.

Live CCTV camera streams can replace prerecorded videos.

Traffic data can be stored for long term analysis.

Machine learning can be used to predict traffic congestion.

The system can be connected to physical traffic signal hardware.

## Conclusion

The Smart Traffic Light System demonstrates how computer vision and intelligent traffic control can be combined to create a dynamic traffic management system.

By continuously detecting vehicles and adjusting signal timings according to traffic density, the system can provide a more responsive approach to traffic signal management.
