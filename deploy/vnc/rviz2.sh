#!/bin/bash
# RViz on the VNC desktop (display :1) with the Nav2 view (map, costmaps, plans, laser scan — the
# displays the ROS 2 course uses), opened by the panel's RViz button (tint2rc) and by the PhysiCar
# panel's Navigation tutorial (▶ SLAM / ▶ Nav2). Neither starts it from a terminal, so it is run
# through an interactive bash: ~/.bashrc gives it exactly the terminal's environment (ROS 2, this
# machine's DDS setup, VirtualGL on the robot). One at a time: with a second instance beside the
# first, one of them aborted within seconds (2026-10-16), so an open RViz is simply kept.
export DISPLAY="${DISPLAY:-:1}"
pgrep -x rviz2 >/dev/null 2>&1 && exit 0
_d="${DISPLAY#:}"; _d="${_d%%.*}"
for _ in $(seq 1 120); do [ -S "/tmp/.X11-unix/X${_d}" ] && break; sleep 0.5; done
exec bash -ic 'exec rviz2 -d /opt/ros/jazzy/share/nav2_bringup/rviz/nav2_default_view.rviz "$@"' rviz2 "$@"
