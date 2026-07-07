#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════════
#  Physicar SIM Boot Script — runs via supervisord
#  Launches ROS2 sim.launch.py + background updater for physicar-ros/sim repos.
# ═══════════════════════════════════════════════════════════════════════════════

PHYSICAR_WS="/opt/physicar"
PHYSICAR_ROS_DIR="$PHYSICAR_WS/src/physicar-ros"
PHYSICAR_SIM_DIR="$PHYSICAR_WS/src/physicar-sim"
PHYSICAR_DIR="$PHYSICAR_WS/userdata"

# Load environment (.env)
ENV_FILE="$PHYSICAR_DIR/.env"
if [ -f "$ENV_FILE" ]; then
    if bash -n "$ENV_FILE" 2>/dev/null; then
        set -a; . "$ENV_FILE"; set +a
    fi
fi

# ────────────────── ROS2 Launch ──────────────────

export DISPLAY=:1
export GZ_PARTITION=physicar
export GZ_CONFIG_PATH=/usr/share/gz
export GALLIUM_DRIVER=llvmpipe
export MESA_GL_VERSION_OVERRIDE=3.3
source /opt/ros/jazzy/setup.bash
export ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST

# Same loopback-pinned DDS profile as the device (fastdds-lo.xml):
# identical transport behavior in SIM and on hardware, and no SHM →
# no Docker /dev/shm pressure.
export FASTRTPS_DEFAULT_PROFILES_FILE="$PHYSICAR_ROS_DIR/deploy/device/fastdds-lo.xml"
rm -f /dev/shm/fastrtps_* 2>/dev/null

UPDATE_SIGNAL="/tmp/.physicar-update-ready"

git config --global --add safe.directory "$PHYSICAR_ROS_DIR" 2>/dev/null || true
git config --global --add safe.directory "$PHYSICAR_SIM_DIR" 2>/dev/null || true

clean_build() {
    echo "[physicar] Running clean build..."
    rm -rf "$PHYSICAR_WS/build" "$PHYSICAR_WS/install" "$PHYSICAR_WS/log"
    cd "$PHYSICAR_WS" && colcon build --symlink-install 2>&1
}

do_build() {
    # SIM doesn't need physicar_camera or physicar_lidar
    touch "$PHYSICAR_ROS_DIR/physicar_camera/COLCON_IGNORE" 2>/dev/null || true
    touch "$PHYSICAR_ROS_DIR/physicar_lidar/COLCON_IGNORE" 2>/dev/null || true

    echo "[physicar] Building..."
    cd "$PHYSICAR_WS" && colcon build --symlink-install 2>&1
    local exit_code=$?

    if [ $exit_code -ne 0 ]; then
        echo "[physicar] Build failed. Retrying clean..."
        clean_build
        exit_code=$?
    fi

    source "$PHYSICAR_WS/install/setup.bash"
    return $exit_code
}

rm -f "$UPDATE_SIGNAL"

if [ ! -d "$PHYSICAR_WS/install" ]; then
    do_build
else
    echo "[physicar] install/ exists, skipping build."
    source "$PHYSICAR_WS/install/setup.bash"
fi

LAUNCH_PID=""
trap 'kill -TERM -$$ 2>/dev/null; exit 0' TERM INT

while true; do
    echo "[physicar] Launching sim..."
    ros2 launch physicar_bringup sim.launch.py &
    LAUNCH_PID=$!
    wait $LAUNCH_PID 2>/dev/null

    if [ -f "$UPDATE_SIGNAL" ]; then
        rm -f "$UPDATE_SIGNAL"
        echo "[physicar] Update detected → rebuilding..."
        do_build
        continue
    fi

    echo "[physicar] Launch exited. Restarting in 3s..."
    sleep 3
done &
ROS_LOOP_PID=$!

# ────────────────── Updater (physicar-ros + physicar-sim) ──────────────────
if [ -f "$PHYSICAR_ROS_DIR/updater.sh" ]; then
  bash "$PHYSICAR_ROS_DIR/updater.sh" &
fi

wait $ROS_LOOP_PID
