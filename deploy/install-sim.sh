#!/usr/bin/env bash
set -euo pipefail

# ═══════════════════════════════════════════════════════════════════════════════
#  Physicar SIM Installer — Ubuntu 24.04
#  Mirrors deploy/install-device.sh pattern but for simulation environment.
#  Works on any Ubuntu 24.04 host (Codespaces, local VM, cloud, etc.)
#
#  Usage:
#    sudo bash /opt/physicar/src/physicar-ros/deploy/install-sim.sh
# ═══════════════════════════════════════════════════════════════════════════════

echo "========== Physicar SIM Setup =========="

export DEBIAN_FRONTEND=noninteractive

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PHYSICAR_ROS_DIR="$(dirname "$SCRIPT_DIR")"
PHYSICAR_WS="$(dirname "$(dirname "$PHYSICAR_ROS_DIR")")"
DEPLOY_DIR="$SCRIPT_DIR/sim"

# ── Helper: wait for apt/dpkg lock ──
wait_for_apt() {
  local max_wait=300 waited=0
  while fuser /var/lib/dpkg/lock-frontend &>/dev/null 2>&1; do
    if [ $waited -ge $max_wait ]; then
      echo "ERROR: dpkg lock held for over ${max_wait}s, aborting."
      exit 1
    fi
    echo "  waiting for dpkg lock (${waited}s)..."
    sleep 5
    waited=$((waited + 5))
  done
}

# ┌─────────────────────────────────────────────────────────────────────────────┐
# │  1. System Packages                                                        │
# └─────────────────────────────────────────────────────────────────────────────┘

echo "[1/7] Installing system packages..."

wait_for_apt
apt-get update -y

wait_for_apt
apt-get install -y \
  curl gnupg2 lsb-release software-properties-common apt-transport-https ca-certificates locales \
  xvfb x11vnc novnc websockify xterm supervisor net-tools \
  jq python3-pip ffmpeg gh mpv \
  python3-fastapi python3-uvicorn \
  nginx openbox tint2 alsa-utils python3-dev \
  fonts-noto fonts-noto-cjk fonts-noto-cjk-extra fonts-noto-color-emoji

# Python bytecode cache -> /tmp (keeps __pycache__ out of the student workspace)
echo 'export PYTHONPYCACHEPREFIX=/tmp/pycache' > /etc/profile.d/pycache.sh

# noVNC symlink + auto-reconnect patch
[ -d /usr/share/novnc ] && ln -sf vnc_lite.html /usr/share/novnc/index.html
sed -i 's|status("Something went wrong, connection is closed");|status("Reconnecting..."); setTimeout(function(){location.reload();},2000); return;|' /usr/share/novnc/vnc_lite.html 2>/dev/null || true
sed -i 's|status("Disconnected");|status("Reconnecting..."); setTimeout(function(){location.reload();},2000); return;|' /usr/share/novnc/vnc_lite.html 2>/dev/null || true

# cloudflared — quick tunnel, used in Codespaces to expose nginx:80 via a
# stable public URL (published as physicarcs.com; GitHub port-forwarding is
# unreliable).
if ! command -v cloudflared &>/dev/null; then
  echo "  Installing cloudflared..."
  CF_ARCH="$(dpkg --print-architecture)"   # amd64 | arm64
  curl -fsSL -o /usr/local/bin/cloudflared \
    "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${CF_ARCH}"
  chmod +x /usr/local/bin/cloudflared
fi

# code-server — serves `/` on a local (non-Codespaces) sim. Installed into
# the image unconditionally; supervisord only starts it when CODESPACE_NAME
# is unset (in Codespaces VS Code web is the Codespace itself).
if ! command -v code-server &>/dev/null; then
  echo "  Installing code-server..."
  curl -fsSL https://code-server.dev/install.sh | sh
fi

# ── Webview microphone/camera patch ──
# VS Code's webview iframes don't delegate mic/cam permission, which blocks
# getUserMedia in every webview below them (extension panels, app.physicar).
# Append 'microphone; camera' to the webview iframe allow-list in the served
# workbench bundle. Idempotent; re-applied at boot too (entrypoint.sh) since
# a code-server update restores the bundle. localhost is a secure context,
# so getUserMedia works on the local-sim http://localhost access path.
patch_codeserver_webview_media() {
  local cs_bin cs_vscode
  cs_bin=$(readlink -f "$(command -v code-server)" 2>/dev/null) || return 0
  cs_vscode=$(dirname "$cs_bin")/../lib/vscode
  [ -d "$cs_vscode/out" ] || cs_vscode=/usr/lib/code-server/lib/vscode
  [ -d "$cs_vscode/out" ] || { echo "[media-patch] vscode bundle not found"; return 0; }

  # Allow-list patterns per code-server generation (each patched idempotently):
  #  A) legacy literal allow string
  #  B) 4.12x workbench JS — allow list built as a JS array
  #  C) 4.12x inner webview iframe (pre/index.html) — allowRules array
  local A_OLD='clipboard-read; clipboard-write'
  local A_NEW='clipboard-read; clipboard-write; microphone; camera'
  local B_OLD='"cross-origin-isolated","autoplay","local-network-access"'
  local B_NEW='"cross-origin-isolated","autoplay","local-network-access","microphone","camera"'
  local C_OLD="'cross-origin-isolated;', 'autoplay;', 'local-network-access;'"
  local C_NEW="'cross-origin-isolated;', 'autoplay;', 'local-network-access;', 'microphone;', 'camera;'"

  local n=0 f changed
  while IFS= read -r f; do
    changed=0
    if grep -qF "$A_OLD" "$f" && ! grep -qF "$A_NEW" "$f"; then
      sed -i "s/$A_OLD/$A_NEW/g" "$f" && changed=1
    fi
    if grep -qF "$B_OLD" "$f" && ! grep -qF "$B_NEW" "$f"; then
      sed -i "s/$B_OLD/$B_NEW/g" "$f" && changed=1
    fi
    if grep -qF "$C_OLD" "$f" && ! grep -qF "$C_NEW" "$f"; then
      sed -i "s|$C_OLD|$C_NEW|g" "$f" && changed=1
    fi
    [ "$changed" = "1" ] && n=$((n+1))
  done < <(grep -rlF -e "$A_OLD" -e "$B_OLD" -e "$C_OLD" "$cs_vscode/out" 2>/dev/null)
  echo "[media-patch] patched $n file(s) under $cs_vscode/out"

  # Silent-failure guard: after patching, at least one file must carry one of
  # the patched allow-lists. If none do, a code-server update changed the
  # pattern shape (it happened at 4.12x already) — warn loudly so it shows up
  # in the boot log instead of mic/cam just silently breaking.
  if ! grep -rqF -e "$A_NEW" -e "$B_NEW" -e "$C_NEW" "$cs_vscode/out" 2>/dev/null; then
    echo "[media-patch] WARNING: no known allow-list pattern found in this code-server version — webview mic/cam will stay blocked until the patterns in this function are updated"
  fi
}
patch_codeserver_webview_media || true

# ┌─────────────────────────────────────────────────────────────────────────────┐
# │  2. ROS 2 Jazzy + Gazebo Harmonic                                         │
# └─────────────────────────────────────────────────────────────────────────────┘

echo "[2/7] Installing ROS 2 Jazzy + Gazebo Harmonic..."

# ROS 2 Jazzy
if [ ! -f /opt/ros/jazzy/setup.bash ]; then
  curl -sSL https://raw.githubusercontent.com/ros/rosdistro/master/ros.key \
    -o /usr/share/keyrings/ros-archive-keyring.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/ros-archive-keyring.gpg] http://packages.ros.org/ros2/ubuntu $(lsb_release -cs) main" \
    | tee /etc/apt/sources.list.d/ros2.list > /dev/null
  wait_for_apt
  apt-get update -y
  wait_for_apt
  apt-get install -y --no-install-recommends \
    ros-jazzy-ros-base \
    ros-jazzy-rviz2 \
    ros-jazzy-rqt \
    ros-jazzy-rqt-common-plugins \
    ros-jazzy-image-transport \
    ros-jazzy-image-transport-plugins \
    ros-jazzy-cv-bridge \
    ros-jazzy-teleop-twist-keyboard \
    ros-jazzy-tf2-tools \
    ros-jazzy-xacro \
    python3-colcon-common-extensions \
    python3-rosdep \
    ros-jazzy-rosbridge-server \
    ros-jazzy-ros2-control \
    ros-jazzy-ros2-controllers
fi

# Gazebo Harmonic + ros-gz bridge
if ! command -v gz &>/dev/null; then
  curl -fsSL https://packages.osrfoundation.org/gazebo.gpg \
    -o /usr/share/keyrings/pkgs-osrf-archive-keyring.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/pkgs-osrf-archive-keyring.gpg] https://packages.osrfoundation.org/gazebo/ubuntu-stable $(lsb_release -cs) main" \
    | tee /etc/apt/sources.list.d/gazebo-stable.list > /dev/null
  wait_for_apt
  apt-get update -y
  wait_for_apt
  apt-get install -y gz-harmonic ros-jazzy-ros-gz
fi

# SLAM/Nav2
wait_for_apt
apt-get install -y --no-install-recommends \
  ros-jazzy-slam-toolbox \
  ros-jazzy-cartographer-ros \
  ros-jazzy-navigation2 \
  ros-jazzy-nav2-bringup \
  ros-jazzy-nav2-rviz-plugins \
  ros-jazzy-rqt-tf-tree \
  ros-jazzy-rqt-graph \
  ros-jazzy-joy \
  ros-jazzy-camera-info-manager

set +u; source /opt/ros/jazzy/setup.bash; set -u
rosdep init 2>/dev/null || true
sudo -u physicar rosdep update --rosdistro jazzy 2>/dev/null || true

# Patch nav2: disable docking_server & route_server
NAV2_LAUNCH=/opt/ros/jazzy/share/nav2_bringup/launch/navigation_launch.py
if grep -q "'route_server'," "$NAV2_LAUNCH" 2>/dev/null; then
  python3 -c "
import re
with open('$NAV2_LAUNCH') as f: c = f.read()
for old, new in [
    (\"        'route_server',\\n\", \"        # 'route_server',  # disabled for physicar\\n\"),
    (\"        'docking_server',\\n\", \"        # 'docking_server',  # disabled for physicar\\n\"),
]:
    c = c.replace(old, new)
for pkg, exe, plugin in [
    ('nav2_route', 'route_server', 'nav2_route::RouteServer'),
    ('opennav_docking', 'opennav_docking', 'opennav_docking::DockingServer'),
]:
    c = re.sub(
        r\"(            )(Node\\(\\n\\s+package='\" + pkg + r\"'.*?remappings=remappings,\\n\\s+\\),)\",
        lambda m: m.group(1) + '# ' + m.group(2).replace('\\n', '\\n' + m.group(1) + '# '),
        c, count=1, flags=re.DOTALL)
    c = re.sub(
        r\"(                    )(ComposableNode\\(\\n\\s+package='\" + pkg + r\"'.*?remappings=remappings,\\n\\s+\\),)\",
        lambda m: m.group(1) + '# ' + m.group(2).replace('\\n', '\\n' + m.group(1) + '# '),
        c, count=1, flags=re.DOTALL)
with open('$NAV2_LAUNCH', 'w') as f: f.write(c)
print('nav2 navigation_launch.py patched')
"
fi

# Lock package versions
apt-mark hold $(dpkg -l | grep -E '^ii  (ros-jazzy|gz-|libgz-)' | awk '{print $2}') 2>/dev/null || true

# ┌─────────────────────────────────────────────────────────────────────────────┐
# │  3. Python Packages                                                        │
# └─────────────────────────────────────────────────────────────────────────────┘

echo "[3/7] Installing Python packages..."

runuser -u physicar -- python3 -m pip config set global.break-system-packages true 2>/dev/null || true

# Pin numpy<2
mkdir -p /etc/pip
ln -sf "$DEPLOY_DIR/etc/pip/constraints.txt" /etc/pip/constraints.txt
ln -sf "$DEPLOY_DIR/etc/pip/pip.conf" /etc/pip/pip.conf
grep -q 'PIP_CONSTRAINT' /etc/environment 2>/dev/null || \
  echo 'PIP_CONSTRAINT=/etc/pip/constraints.txt' >> /etc/environment
export PIP_CONSTRAINT=/etc/pip/constraints.txt

sudo -u physicar PIP_CONSTRAINT=/etc/pip/constraints.txt python3 -m pip install --user \
  'physicar~=1.0' \
  'flask~=3.1' \
  'flask-cors~=4.0' \
  'flask-sock~=0.7' \
  'requests~=2.32' \
  'ultralytics~=8.4' \
  'numpy<2' \
  opencv-python-headless==4.9.0.80 \
  websockets aiohttp \
  'ddgs~=9.14' \
  python-multipart watchdog pydantic starlette \
  'tensorflow==2.17.1' \
  setuptools==70.0.0

# ── TFLite C++ headers & library (for physicar_deepracer C++ node) ──
echo "  Installing TFLite C++ headers..."

TF_VER=$(sudo -u physicar python3 -c "import tensorflow; print(tensorflow.__version__)" 2>/dev/null || echo "2.17.1")
TF_LIB=$(sudo -u physicar python3 -c "import tensorflow, os; print(os.path.dirname(tensorflow.__file__))" 2>/dev/null)

# 1) TFLite C++ headers from tensorflow source (sparse checkout, arch-independent)
if [ ! -f /usr/local/include/tensorflow/lite/interpreter.h ]; then
  TMPD=$(mktemp -d)
  cd "$TMPD"
  git init -q
  git remote add origin https://github.com/tensorflow/tensorflow.git
  git config core.sparseCheckout true
  echo "tensorflow/lite/" > .git/info/sparse-checkout
  git fetch --depth 1 origin "v${TF_VER}" 2>/dev/null
  git checkout FETCH_HEAD -- tensorflow/lite/ 2>/dev/null
  cp -r tensorflow /usr/local/include/
  chmod -R a+rX /usr/local/include/tensorflow/
  rm -rf "$TMPD"
  cd /
fi

# 2) flatbuffers headers (required by TFLite schema)
if [ ! -f /usr/local/include/flatbuffers/flatbuffers.h ]; then
  FLATBUF_VER="24.3.25"
  TMPD=$(mktemp -d)
  curl -sL "https://github.com/google/flatbuffers/archive/refs/tags/v${FLATBUF_VER}.tar.gz" | tar xz -C "$TMPD"
  cp -r "$TMPD/flatbuffers-${FLATBUF_VER}/include/flatbuffers" /usr/local/include/
  chmod -R a+rX /usr/local/include/flatbuffers/
  rm -rf "$TMPD"
fi

# 3) Symlink pip tensorflow's libtensorflow_cc.so.2 → /usr/local/lib
#    (contains all TFLite symbols; SONAME is libtensorflow_cc.so.2)
if [ -n "$TF_LIB" ] && [ -f "$TF_LIB/libtensorflow_cc.so.2" ]; then
  ln -sf "$TF_LIB/libtensorflow_cc.so.2" /usr/local/lib/libtensorflowlite.so
  ln -sf "$TF_LIB/libtensorflow_cc.so.2" /usr/local/lib/libtensorflow_cc.so.2
  ln -sf "$TF_LIB/libtensorflow_framework.so.2" /usr/local/lib/libtensorflow_framework.so.2
  ldconfig
fi

# ┌─────────────────────────────────────────────────────────────────────────────┐
# │  4. Config Deployment (symlinks)                                           │
# └─────────────────────────────────────────────────────────────────────────────┘

echo "[4/7] Deploying config files..."

# Openbox / tint2
mkdir -p /etc/xdg/openbox /home/physicar/.config/tint2
ln -sf "$DEPLOY_DIR/etc/xdg/openbox/rc.xml" /etc/xdg/openbox/rc.xml
ln -sf "$DEPLOY_DIR/home/physicar/.config/tint2/tint2rc" /home/physicar/.config/tint2/tint2rc
chown -R physicar:physicar /home/physicar/.config
ln -sf "$DEPLOY_DIR/usr/share/applications/xterm.desktop" /usr/share/applications/xterm.desktop

# Nginx
rm -f /etc/nginx/sites-enabled/default
ln -sf "$DEPLOY_DIR/etc/nginx/sites-available/physicar" /etc/nginx/sites-available/physicar
ln -sf /etc/nginx/sites-available/physicar /etc/nginx/sites-enabled/physicar
# Studio access-token gate include (static; `include`s /tmp/pc-token.map, which
# app-browser.sh fills with the per-session token at runtime).
ln -sf "$DEPLOY_DIR/etc/nginx/conf.d/pc-token.conf" /etc/nginx/conf.d/pc-token.conf
# Empty token map so nginx can load now (entrypoint.sh recreates it on boot).
touch /tmp/pc-token.map
# Root (/) snippet so nginx can load now — entrypoint.sh rewrites it on boot
# (root-code.conf on a local sim, root-404.conf in Codespaces). Copied, not
# symlinked: fs.protected_symlinks blocks cross-owner symlinks in sticky /tmp.
cp -f "$DEPLOY_DIR/etc/nginx/root-404.conf" /tmp/pc-root.conf

# supervisord log directory
mkdir -p /var/log/supervisor
chown -R physicar:physicar /var/log/supervisor

# Script permissions
chmod +x "$DEPLOY_DIR/physicar.sh"
chmod +x "$DEPLOY_DIR/app-browser.sh" 2>/dev/null || true

# ┌─────────────────────────────────────────────────────────────────────────────┐
# │  5. Workspace Setup                                                        │
# └─────────────────────────────────────────────────────────────────────────────┘

echo "[5/7] Workspace setup..."

# /opt/physicar/userdata .env (same path as device)
mkdir -p "$PHYSICAR_WS/userdata"
echo "SIM=true" | tee "$PHYSICAR_WS/userdata/.env" > /dev/null
# Own the whole userdata dir (not just .env) — supervisord runs the `physicar`
# program as the physicar user and writes physicar.log here; a root-owned dir
# causes EACCES and the program fails to spawn (FATAL).
chown -R physicar:physicar "$PHYSICAR_WS/userdata"

# COLCON_IGNORE for device-only packages
touch "$PHYSICAR_ROS_DIR/physicar_camera/COLCON_IGNORE" 2>/dev/null || true
touch "$PHYSICAR_ROS_DIR/physicar_lidar/COLCON_IGNORE" 2>/dev/null || true

# Symlink ~/physicar-ros for convenience
sudo -u physicar ln -sfn "$PHYSICAR_ROS_DIR" /home/physicar/physicar-ros

# git safe directories
sudo -u physicar git config --global --add safe.directory "$PHYSICAR_ROS_DIR"
sudo -u physicar git config --global --add safe.directory "$PHYSICAR_WS/src/physicar-sim"

# ┌─────────────────────────────────────────────────────────────────────────────┐
# │  6. Bashrc + Build                                                         │
# └─────────────────────────────────────────────────────────────────────────────┘

echo "[6/7] Bashrc + initial build..."

# Append bashrc (idempotent)
MARKER="# physicar-sim-env"
if ! grep -q "$MARKER" /home/physicar/.bashrc 2>/dev/null; then
  echo "$MARKER" >> /home/physicar/.bashrc
  cat "$DEPLOY_DIR/home/physicar/bashrc-append" >> /home/physicar/.bashrc
fi

# Allow nginx (www-data) to traverse /home/physicar
chmod o+x /home/physicar

# sudoers
echo 'physicar ALL=(ALL) NOPASSWD: ALL' > /etc/sudoers.d/physicar
chmod 440 /etc/sudoers.d/physicar

# Initial colcon build
sudo -u physicar bash -c 'set +u; source /opt/ros/jazzy/setup.bash; set -u; cd '"$PHYSICAR_WS"' && colcon build --symlink-install'
rm -rf "$PHYSICAR_WS/log"

# ┌─────────────────────────────────────────────────────────────────────────────┐
# │  7. Cleanup                                                                │
# └─────────────────────────────────────────────────────────────────────────────┘

echo "[7/7] Cleanup..."

apt-get clean
rm -rf /var/lib/apt/lists/*

echo ""
echo "=========================================="
echo "      Physicar SIM Setup Complete        "
echo "=========================================="
echo ""
