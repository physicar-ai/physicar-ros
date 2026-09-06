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

# Same loopback-pinned CycloneDDS setup as the real kit (deploy/cyclonedds.xml)
# — identical middleware and transport behavior in SIM and on hardware.
export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
export CYCLONEDDS_URI="file://$PHYSICAR_ROS_DIR/deploy/cyclonedds.xml"

UPDATE_SIGNAL="/tmp/.physicar-update-ready"
BUILD_LOCK="/tmp/.physicar-build.lock"
# Marker the updater keeps while its own build is in flight (see updater.sh)
UPDATER_BUILDING="/tmp/.physicar-build-pending"

# Boot progress for the nginx offline pages (served verbatim at /__boot):
# updating -> building -> starting. Best-effort — must never break the boot.
BOOT_STATUS="/tmp/.physicar-boot-status"
boot_status() {
    printf '{"phase":"%s","since":%s}' "$1" "$(date +%s)" > "${BOOT_STATUS}.tmp" 2>/dev/null \
        && mv -f "${BOOT_STATUS}.tmp" "$BOOT_STATUS" 2>/dev/null || true
}

git config --global --add safe.directory "$PHYSICAR_ROS_DIR" 2>/dev/null || true
git config --global --add safe.directory "$PHYSICAR_SIM_DIR" 2>/dev/null || true

clean_build() {
    echo "[physicar] Running clean build..."
    boot_status building
    rm -rf "$PHYSICAR_WS/build" "$PHYSICAR_WS/install" "$PHYSICAR_WS/log"
    cd "$PHYSICAR_WS" && colcon build --symlink-install 2>&1
}

do_build() {
    # SIM doesn't need physicar_camera or physicar_lidar
    touch "$PHYSICAR_ROS_DIR/physicar_camera/COLCON_IGNORE" 2>/dev/null || true
    touch "$PHYSICAR_ROS_DIR/physicar_lidar/COLCON_IGNORE" 2>/dev/null || true

    echo "[physicar] Building..."
    boot_status building
    (
        # Serialize with the updater's safe_build (same lock file): two
        # concurrent colcon builds in one workspace corrupt each other.
        flock -x 200
        cd "$PHYSICAR_WS" && colcon build --symlink-install 2>&1 || {
            echo "[physicar] Build failed. Retrying clean..."
            clean_build
        }
    ) 200>"$BUILD_LOCK"
    local exit_code=$?

    # Stamp WHICH revision this install was built from — verify_install
    # compares it to the source HEAD, so a checkout without a (successful)
    # rebuild self-heals at the next boot instead of crash-looping on
    # imports of not-yet-installed modules (real incident 2026-09-05).
    if [ $exit_code -eq 0 ]; then
        git -C "$PHYSICAR_ROS_DIR" rev-parse HEAD > "$PHYSICAR_WS/install/.physicar-head" 2>/dev/null || true
    fi

    source "$PHYSICAR_WS/install/setup.bash"
    return $exit_code
}

# Boot-time update: with internet, update to the latest before first run (updater.sh --boot).
if [ -f "$PHYSICAR_ROS_DIR/updater.sh" ]; then
    _pre_head=$(git -C "$PHYSICAR_ROS_DIR" rev-parse HEAD 2>/dev/null || true)
    boot_status updating
    bash "$PHYSICAR_ROS_DIR/updater.sh" --boot
    _post_head=$(git -C "$PHYSICAR_ROS_DIR" rev-parse HEAD 2>/dev/null || true)
    # 부팅 중 코드가 갱신됐으면, 이미 옛 코드로 한 번 돌아버린 부팅 시점 프로그램
    # (app_browser: 우선순위 250 < physicar 650)을 새 코드로 다시 돌린다 — 릴리스가
    # "둘째 부팅부터"가 아니라 첫 부팅부터 반영되게. (실사례 2026-08-22: 북마크 가드가
    # 태그 배포 후 첫 부팅엔 옛 스크립트가 먼저 써 한 부팅 늦게 적용됐다)
    if [ -n "$_post_head" ] && [ "$_pre_head" != "$_post_head" ]; then
        echo "[physicar] code updated during boot (${_pre_head:0:7} -> ${_post_head:0:7}) — re-running app_browser"
        supervisorctl -s unix:///tmp/supervisor.sock restart app_browser >/dev/null 2>&1 || true
    fi
fi

rm -f "$UPDATE_SIGNAL"

if [ ! -d "$PHYSICAR_WS/install" ]; then
    do_build
else
    echo "[physicar] install/ exists, skipping build."
    source "$PHYSICAR_WS/install/setup.bash"
fi

# Executables sim.launch.py needs. When any is missing, install/ is stale
# (typically: source pulled without a rebuild) — self-heal with a build
# instead of letting the launch fail.
REQUIRED_EXECUTABLES=(
    "physicar_bringup/lib/physicar_bringup/scan_filter_node"
    "physicar_bringup/lib/physicar_bringup/topic_watchdog_node"
    "physicar_bringup/lib/physicar_bringup/cmd_vel_adapter_node.py"
    "physicar_webserver/lib/physicar_webserver/webserver_node.py"
    "physicar_laser_odom/lib/physicar_laser_odom/laser_odom_node"
)

verify_install() {
    local missing=0 e
    for e in "${REQUIRED_EXECUTABLES[@]}"; do
        if [ ! -x "$PHYSICAR_WS/install/$e" ]; then
            echo "[physicar] install/$e is missing"
            missing=1
        fi
    done
    # install must have been built from the CURRENT source revision — a
    # checkout that never got its rebuild otherwise crash-loops on imports
    # of modules that are not in install yet (see the do_build stamp)
    local want have
    want=$(git -C "$PHYSICAR_ROS_DIR" rev-parse HEAD 2>/dev/null)
    have=$(cat "$PHYSICAR_WS/install/.physicar-head" 2>/dev/null)
    if [ -n "$want" ] && [ "$want" != "$have" ]; then
        echo "[physicar] install/ was built from a different revision (${have:-none})"
        missing=1
    fi
    return $missing
}

cleanup_stray_nodes() {
    # A crashed launch can leave orphaned node processes behind. Every orphan
    # is a live DDS participant that keeps the domain's ports and discovery
    # state busy; enough of them and new nodes fail with "rmw_create_node:
    # failed to create domain". Sweep them before the next launch.
    # Never sweep while the updater is mid-build: compiler/cmake children can
    # carry install/ paths on their command lines and would match the pattern.
    [ -f "$UPDATER_BUILDING" ] && return 0
    pkill -f "$PHYSICAR_WS/install" 2>/dev/null
    pkill -f "launch_params_" 2>/dev/null
    pkill -f "image_transport republish" 2>/dev/null
    sleep 1
}

LAUNCH_PID=""
trap 'kill -TERM -$$ 2>/dev/null; exit 0' TERM INT

boot_status starting
FAIL_STREAK=0
while true; do
    if ! verify_install; then
        echo "[physicar] install/ is stale or incomplete → rebuilding..."
        do_build
    fi

    echo "[physicar] Launching sim..."
    LAUNCH_T0=$SECONDS
    # Re-source per launch: a rebuild may have added packages whose prefixes
    # were not in the environment sourced at boot.
    ( source "$PHYSICAR_WS/install/setup.bash" 2>/dev/null
      exec ros2 launch physicar_bringup sim.launch.py ) &
    LAUNCH_PID=$!
    wait $LAUNCH_PID 2>/dev/null

    cleanup_stray_nodes

    if [ -f "$UPDATE_SIGNAL" ]; then
        rm -f "$UPDATE_SIGNAL"
        echo "[physicar] Update detected → rebuilding..."
        do_build
        FAIL_STREAK=0
        continue
    fi

    # Exponential backoff on rapid crash loops; reset after a healthy run.
    if [ $((SECONDS - LAUNCH_T0)) -ge 60 ]; then
        FAIL_STREAK=0
    fi
    DELAY=$((3 * (1 << FAIL_STREAK)))
    [ "$FAIL_STREAK" -lt 4 ] && FAIL_STREAK=$((FAIL_STREAK + 1))
    echo "[physicar] Launch exited. Restarting in ${DELAY}s..."
    sleep $DELAY
done &
ROS_LOOP_PID=$!

# ────────────────── Updater (physicar-ros + physicar-sim) ──────────────────
if [ -f "$PHYSICAR_ROS_DIR/updater.sh" ]; then
  bash "$PHYSICAR_ROS_DIR/updater.sh" &
fi

wait $ROS_LOOP_PID
