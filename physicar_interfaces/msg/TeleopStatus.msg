# TeleopStatus
#
# Generic teleoperation status published on /teleop/status by *any* teleop
# source (joy_teleop, keyboard_teleop, web teleop, ...).  Consumers
# (driver, cmd_vel_adapter, UI) treat all sources uniformly.
#
# Freshness:
#   The publisher MUST republish at least every `timeout` interval.
#   Consumers should treat the status as expired (drive_engaged /
#   camera_engaged = false, no source engaged) once
#   `now - last_received > timeout`.
#
#   Recommended publish rate: 10 Hz (timeout = 500 ms).
#
# Fields:
#   source         - human-readable name of the publishing teleop
#                    ("joy", "keyboard", "web", ...).  Mostly for UI
#                    and diagnostics; the driver does not switch logic
#                    on it.
#   drive_engaged   - this source is currently driving the vehicle.
#                    Driver MUST ignore /speed, /steering and /cmd_vel
#                    and only honour /teleop/speed, /teleop/steering
#                    while drive_engaged is true and fresh.
#   camera_engaged  - same but for /camera/pan and /camera/tilt vs
#                    /teleop/camera/pan,/tilt.
#   estop_latched  - the source has latched an emergency stop (drive
#                    will be held at zero).
#   timeout        - how long this status is valid for; consumers should
#                    treat both teleop_* flags as released once this
#                    elapses without a refresh.

string source
bool drive_engaged
bool camera_engaged
bool estop_latched
builtin_interfaces/Duration timeout
