#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════════
#  Physicar SIM Boot Script — runs via supervisord
#  Launches ROS2 sim.launch.py + background updater for physicar-ros/sim repos.
# ═══════════════════════════════════════════════════════════════════════════════

PHYSICAR_WS="/opt/physicar"
PHYSICAR_ROS_DIR="$PHYSICAR_WS/src/physicar-ros"
PHYSICAR_SIM_DIR="$PHYSICAR_WS/src/physicar-sim"

source "$PHYSICAR_WS/.env" 2>/dev/null || true

# ────────────────── ROS2 Launch ──────────────────

export DISPLAY=:1
export GZ_PARTITION=physicar
export GZ_CONFIG_PATH=/usr/share/gz
export GALLIUM_DRIVER=llvmpipe
export MESA_GL_VERSION_OVERRIDE=3.3
source /opt/ros/jazzy/setup.bash
export ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST

UPDATE_SIGNAL="/tmp/.physicar-update-ready"

git config --global --add safe.directory "$PHYSICAR_ROS_DIR" 2>/dev/null || true
git config --global --add safe.directory "$PHYSICAR_SIM_DIR" 2>/dev/null || true

clean_build() {
    echo "[physicar] Running clean build..."
    rm -rf "$PHYSICAR_WS/build" "$PHYSICAR_WS/install" "$PHYSICAR_WS/log"
    cd "$PHYSICAR_WS" && colcon build --symlink-install 2>&1
}

do_build() {
    # SIM doesn't need camera_ros or rplidar_ros
    touch "$PHYSICAR_ROS_DIR/camera_ros/COLCON_IGNORE" 2>/dev/null || true
    touch "$PHYSICAR_ROS_DIR/rplidar_ros/COLCON_IGNORE" 2>/dev/null || true

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

# ────────────────── Updater (physicar-sim) ──────────────────

(
  sleep 60
  while true; do
    for repo_dir in "$PHYSICAR_SIM_DIR"; do
      [ -d "$repo_dir/.git" ] || continue

      # Clean stale git locks
      for lock in "$repo_dir/.git/index.lock" "$repo_dir/.git/HEAD.lock"; do
        if [ -f "$lock" ]; then
          lock_age=$(( $(date +%s) - $(stat -c %Y "$lock" 2>/dev/null || echo 0) ))
          (( lock_age > 300 )) && rm -f "$lock"
        fi
      done

      timeout 30 git -c gc.auto=0 -C "$repo_dir" fetch --tags 2>/dev/null || continue
      latest=$(git -C "$repo_dir" tag -l 'v1.*' --sort=-v:refname | head -1)
      [ -z "$latest" ] && continue

      current=$(git -C "$repo_dir" rev-parse HEAD 2>/dev/null)
      target=$(git -C "$repo_dir" rev-parse "$latest^{}" 2>/dev/null)
      [ "$current" = "$target" ] && continue

      echo "[physicar] Updating $(basename "$repo_dir") → $latest"
      git -c gc.auto=0 -c advice.detachedHead=false -C "$repo_dir" checkout -f "$latest" 2>/dev/null
    done
    sleep 180
  done
) &

# physicar-ros updater (triggers rebuild)
if [ -f "$PHYSICAR_ROS_DIR/updater.sh" ]; then
  bash "$PHYSICAR_ROS_DIR/updater.sh" &
fi

wait $ROS_LOOP_PID
