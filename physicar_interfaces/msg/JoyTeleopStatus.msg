# JoyTeleopStatus
#
# Joy-specific status for the joy_teleop node.  Published on
# /physicar_joy_teleop/status.  The generic teleop lock state lives in
# TeleopStatus on /teleop/status — this message carries only fields
# that are meaningful for a gamepad source.
#
# Fields:
#   enabled - master switch.  When false, joy_teleop publishes nothing
#             on /teleop/* and reports drive_engaged=false in TeleopStatus.

bool enabled
