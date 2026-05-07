#!/bin/bash

# Load environment first (SIM, DEV flags needed before build).
# A corrupt .env shouldn't take down the container — just skip it and
# the user can fix the file directly.
ENV_FILE=/opt/physicar/.env
if [ -f "$ENV_FILE" ]; then
    if bash -n "$ENV_FILE" 2>/dev/null; then
        set -a; . "$ENV_FILE"; set +a
    else
        echo "[entrypoint] $ENV_FILE has syntax errors, skipping" >&2
    fi
fi

# Source ROS2 environment
source /opt/ros/jazzy/setup.bash
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROS2_WS="${ROS2_WS:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
UPDATE_SIGNAL="/tmp/.physicar-update-ready"

# --- Build with Recovery ---
clean_build() {
    echo "[entrypoint] Running clean build..."
    rm -rf "$ROS2_WS/build" "$ROS2_WS/install" "$ROS2_WS/log"
    cd "$ROS2_WS" && colcon build --symlink-install 2>&1
}

do_build() {
    # Set COLCON_IGNORE markers based on environment
    if [ "$SIM" = "true" ]; then
        touch "$ROS2_WS/src/physicar-ros/camera_ros/COLCON_IGNORE" 2>/dev/null
        touch "$ROS2_WS/src/physicar-ros/rplidar_ros/COLCON_IGNORE" 2>/dev/null
    else
        rm -f "$ROS2_WS/src/physicar-ros/camera_ros/COLCON_IGNORE" 2>/dev/null
        rm -f "$ROS2_WS/src/physicar-ros/rplidar_ros/COLCON_IGNORE" 2>/dev/null
    fi

    echo "[entrypoint] Building..."
    cd "$ROS2_WS" && colcon build --symlink-install 2>&1
    local exit_code=$?

    if [ $exit_code -eq 0 ]; then
        echo "[entrypoint] Build succeeded."
    else
        echo "[entrypoint] Build failed (exit $exit_code). Retrying with clean build..."
        clean_build
        exit_code=$?
        if [ $exit_code -eq 0 ]; then
            echo "[entrypoint] Clean build succeeded."
        else
            echo "[entrypoint] Clean build also failed (exit $exit_code). Check logs."
        fi
    fi

    source "$ROS2_WS/install/setup.bash"
    return $exit_code
}

# DDS: UDP-only on loopback for host-container communication
# - SHM disabled (works across UID boundaries: container root ↔ host physicar)
# - 127.0.0.1 only (no WiFi leak, multiple kits on same network won't conflict)
export FASTRTPS_DEFAULT_PROFILES_FILE="$SCRIPT_DIR/fastdds-lo.xml"
export ROS_AUTOMATIC_DISCOVERY_RANGE=SUBNET

# Start background updater (non-DEV mode only)
if [ "$DEV" != "true" ] && [ -f "$SCRIPT_DIR/updater.sh" ]; then
    echo "[entrypoint] Starting background updater..."
    bash "$SCRIPT_DIR/updater.sh" &
    UPDATER_PID=$!
fi

# ── Build & Launch loop ─────────────────────────────────
# updater.sh will pkill the launch process and touch UPDATE_SIGNAL
# when a new version is available, causing this loop to rebuild and relaunch.
rm -f "$UPDATE_SIGNAL"

while true; do
    do_build

    echo "[entrypoint] Launching..."
    if [ "$SIM" = "true" ]; then
        ros2 launch physicar_bringup sim.launch.py &
    else
        ros2 launch physicar_bringup robot.launch.py &
    fi
    LAUNCH_PID=$!
    wait $LAUNCH_PID 2>/dev/null

    # Check if this was an update-triggered restart
    if [ -f "$UPDATE_SIGNAL" ]; then
        rm -f "$UPDATE_SIGNAL"
        echo "[entrypoint] Update detected → rebuilding..."
        continue
    fi

    # Launch exited without update signal — keep container alive for debugging
    echo "[entrypoint] Launch exited. Container staying alive for debugging."
    exec sleep infinity
done
