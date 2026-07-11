#!/usr/bin/env bash
set -euo pipefail

# Verify physicar user exists
if ! id -u physicar &>/dev/null; then
  echo "ERROR: 'physicar' user not found. Create it first:"
  echo "  sudo adduser physicar && sudo usermod -aG sudo physicar"
  exit 1
fi

echo "========== Physicar Host Setup =========="

export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PHYSICAR_ROS_DIR="$(dirname "$SCRIPT_DIR")"
PHYSICAR_WS="$(dirname "$(dirname "$PHYSICAR_ROS_DIR")")"
DEPLOY_DIR="$SCRIPT_DIR/device"

# ── Helper: wait for apt/dpkg lock before running apt commands ──
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
# │  1. System Configuration                                                   │
# └─────────────────────────────────────────────────────────────────────────────┘

echo "[1/7] System configuration..."

# Limit journald log size (protect SD card lifespan)
sed -i 's/^#SystemMaxUse=.*/SystemMaxUse=50M/' /etc/systemd/journald.conf

# Kernel UDP buffers for DDS. The boot-time SEDP burst of 13+ localhost
# participants overflows the 208KB default (UdpRcvbufErrors) and endpoints
# never match — topics look alive but carry no data.
tee /etc/sysctl.d/99-physicar-dds.conf >/dev/null <<'__SYSCTL__'
net.core.rmem_max=16777216
net.core.rmem_default=4194304
net.core.wmem_max=16777216
__SYSCTL__
sysctl --system >/dev/null 2>&1 || true

# ── Deploy config files ──

ln -sf "$DEPLOY_DIR/etc/security/pwquality.conf" /etc/security/pwquality.conf

mkdir -p /etc/X11/xorg.conf.d

ln -sf "$DEPLOY_DIR/etc/X11/Xwrapper.config" /etc/X11/Xwrapper.config
ln -sf "$DEPLOY_DIR/etc/X11/xorg.conf.d/10-no-dpms.conf" /etc/X11/xorg.conf.d/10-no-dpms.conf

ln -sf "$DEPLOY_DIR/etc/udev/rules.d/99-physicar.rules" /etc/udev/rules.d/99-physicar.rules

# USB modeswitch configs
# Written inline (not shipped as a file) because the config name is "VID:PID"
# and the colon is an invalid filename character on Windows, which breaks
# extraction of the repo ZIP.
mkdir -p /etc/usb_modeswitch.d
cat > "/etc/usb_modeswitch.d/0bda:1a2b" <<'EOF'
# Realtek USB WiFi Adapter — eject mass storage mode
StandardEject=1
EOF

udevadm control --reload-rules
udevadm trigger

# Disable display overscan
if ! grep -q "disable_overscan=1" /boot/firmware/config.txt 2>/dev/null; then
  echo "disable_overscan=1" | tee -a /boot/firmware/config.txt
fi

# ── Hardware PWM for steering + speed ──
# RPi5 RP1: the stock device-tree ships with pwm@98000 disabled.
# This overlay enables it and muxes two header pins:
#   GPIO12 (board pin 32) alt0 = PWM0 ch0 → steering servo
#   GPIO13 (board pin 33) alt0 = PWM0 ch1 → ESC speed
dtc -@ -I dts -O dtb \
  -o /boot/firmware/overlays/pwm0-gpio13.dtbo \
  "$DEPLOY_DIR/boot/firmware/overlays/pwm0-gpio13.dts"
if ! grep -q "dtoverlay=pwm0-gpio13" /boot/firmware/config.txt 2>/dev/null; then
  echo -e "\n# Hardware PWM0: steering (GPIO12) + speed (GPIO13)" \
    | tee -a /boot/firmware/config.txt
  echo "dtoverlay=pwm0-gpio13" | tee -a /boot/firmware/config.txt
fi

# ┌─────────────────────────────────────────────────────────────────────────────┐
# │  2. Package Installation                                                   │
# └─────────────────────────────────────────────────────────────────────────────┘

echo "[2/7] Installing packages..."

# ── Disable automatic updates (ensure post-release stability) ──
systemctl stop unattended-upgrades 2>/dev/null || true
systemctl disable unattended-upgrades 2>/dev/null || true
systemctl disable --now apt-daily.timer apt-daily-upgrade.timer 2>/dev/null || true
systemctl mask apt-daily.service apt-daily-upgrade.service 2>/dev/null || true

tee /etc/apt/apt.conf.d/10physicar-no-autoupdate >/dev/null < "$DEPLOY_DIR/etc/apt/apt.conf.d/10physicar-no-autoupdate"
[ -f /etc/apt/apt.conf.d/20auto-upgrades ] && \
  mv /etc/apt/apt.conf.d/20auto-upgrades /etc/apt/apt.conf.d/20auto-upgrades.disabled || true

# Block snap automatic refresh
snap set system refresh.hold="forever" 2>/dev/null || true
snap set system refresh.timer="fri,sat,sun3,4,5" 2>/dev/null || true
snap refresh --hold 2>/dev/null || true

# Add noble-updates repo (prevent version mismatch with pre-installed packages)
if ! grep -q 'noble-updates' /etc/apt/sources.list.d/ubuntu.sources 2>/dev/null; then
  cat >> /etc/apt/sources.list.d/ubuntu.sources <<'__NOBLE_UPDATES__'

Types: deb
URIs: http://ports.ubuntu.com/ubuntu-ports/
Suites: noble-updates
Components: main restricted universe multiverse
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg
__NOBLE_UPDATES__
fi

wait_for_apt
apt-get update -y
apt-get remove -y unattended-upgrades needrestart 2>/dev/null || true

# ── Kernel upgrade (do this BEFORE installing DKMS modules) ──
# Upgrade kernel+headers first so DKMS builds target the new kernel.
wait_for_apt
apt-get install -y linux-image-raspi linux-headers-raspi
NEW_KVER=$(ls /lib/modules/ | sort -V | tail -1)
echo "  Kernel target: $NEW_KVER (running: $(uname -r))"

wait_for_apt
apt-get install -y \
  openssh-server net-tools iw network-manager \
  bluez bluez-tools \
  dkms git \
  ca-certificates curl jq avahi-daemon \
  xorg xinit unclutter-xfixes feh mesa-utils \
  fonts-noto fonts-noto-cjk fonts-noto-cjk-extra fonts-noto-color-emoji \
  nginx openssl \
  xvfb x11vnc novnc openbox tint2 xterm \
  i2c-tools libi2c-dev v4l-utils \
  alsa-utils mpv \
  ffmpeg \
  gpiod python3-libgpiod libgpiod-dev \
  python3-fastapi python3-uvicorn \
  meson ninja-build python3-ply python3-jinja2 \
  parted pigz pv exfatprogs wget

# Python bytecode cache -> /tmp (keeps __pycache__ out of the student workspace)
echo 'export PYTHONPYCACHEPREFIX=/tmp/pycache' > /etc/profile.d/pycache.sh

# ── PiShrink: shrink SD card images (used by create-device-image.sh) ──
if [ ! -x /usr/local/bin/pishrink.sh ]; then
  wget -qO /usr/local/bin/pishrink.sh \
    https://raw.githubusercontent.com/Drewsif/PiShrink/master/pishrink.sh
  chmod +x /usr/local/bin/pishrink.sh
fi

# ── VirtualGL: forward GPU rendering to noVNC (:1) ──
# Xvfb(:1) renders with llvmpipe (CPU), making 3D apps like rviz2 slow.
# VirtualGL borrows the V3D GPU on Xorg(:0) and sends pixels to :1.
# Auto-enabled via LD_PRELOAD in bashrc-append and physicar.sh — no manual vglrun needed.
VGL_VER="3.1.4"
if ! dpkg -s virtualgl &>/dev/null; then
  curl -fLo /tmp/virtualgl_${VGL_VER}_arm64.deb \
    "https://github.com/VirtualGL/virtualgl/releases/download/${VGL_VER}/virtualgl_${VGL_VER}_arm64.deb"
  dpkg -i /tmp/virtualgl_${VGL_VER}_arm64.deb
  rm -f /tmp/virtualgl_${VGL_VER}_arm64.deb
fi

# Enable Bluetooth daemon (RPi5 onboard BCM chip)
systemctl enable --now bluetooth 2>/dev/null || true

# ── xpadneo: Xbox One/Series controller BLE-HID driver ──
# Build for the newest installed kernel (NEW_KVER), which is what boots after the
# reboot. NOTE: at install time `uname -r` is still the image's stock kernel, and
# we only installed headers for NEW_KVER — so building for the running kernel
# fails. We must pass `-k "$NEW_KVER"` to the *build* step (not just install).
if ! dkms status 2>/dev/null | grep -q "^hid-xpadneo.*installed"; then
  TMPD=$(mktemp -d)
  git clone --depth 1 --branch v0.11-pre https://github.com/atar-axis/xpadneo.git "$TMPD/xpadneo"
  git config --global --add safe.directory "$TMPD/xpadneo"
  # install.sh registers the source + dkms.conf (its own build targets the running
  # kernel and may fail — that's fine, we rebuild for NEW_KVER below).
  ( cd "$TMPD/xpadneo" && ./install.sh ) || true
  XPADNEO_VER=$(dkms status hid-xpadneo 2>/dev/null | sed -n 's#^hid-xpadneo[/,] *\([^:,]*\).*#\1#p' | head -1 | tr -d ' ')
  if [ -n "$XPADNEO_VER" ]; then
    dkms build   "hid-xpadneo/$XPADNEO_VER" -k "$NEW_KVER" || true
    dkms install --force "hid-xpadneo/$XPADNEO_VER" -k "$NEW_KVER" || true
  fi
  rm -rf "$TMPD"
fi

# ── Realtek USB WiFi drivers (DKMS) ──
echo "Installing Realtek USB WiFi drivers (DKMS)..."

declare -A WIFI_DRIVERS=(
  [rtl8852au]="https://github.com/lwfinger/rtl8852au.git"
  [rtl8852bu]="https://github.com/lwfinger/rtl8852bu.git"
  [rtl8852cu]="https://github.com/morrownr/rtl8852cu-20251113.git"
)

for repo in "${!WIFI_DRIVERS[@]}"; do
  KMOD="${repo#rtl}"   # 8852au, 8852bu, or 8852cu
  if dkms status 2>/dev/null | grep -q "^${repo}.*installed"; then
    echo "  $KMOD (DKMS) already installed, skipping."
    continue
  fi
  TMPD=$(mktemp -d)
  git clone --depth 1 "${WIFI_DRIVERS[$repo]}" "$TMPD/$repo"
  # Patch USB ID table: add known adapters whose VID:PID is not upstream yet.
  # This ensures driver_info (chip type) is set correctly at compile time.
  _intf="$TMPD/$repo/os_dep/linux/usb_intf.c"
  if [ -f "$_intf" ] && [ "$repo" = "rtl8852bu" ]; then
    grep -q "0x35bc.*0x0108" "$_intf" || \
      sed -i '/CONFIG_RTL8852B \*\//i\\t/*=== TP-Link TX20U Nano ===*/\n\t{USB_DEVICE_AND_INTERFACE_INFO(0x35bc, 0x0108, 0xff, 0xff, 0xff), .driver_info = RTL8852B},' "$_intf"
  fi
  # Ensure dkms.conf exists with ARCH=arm64
  if [ ! -f "$TMPD/$repo/dkms.conf" ]; then
    VER="1.0.0"
    cat > "$TMPD/$repo/dkms.conf" <<DKMS
PACKAGE_NAME="$repo"
PACKAGE_VERSION="$VER"
MAKE="'make' ARCH=arm64 -j\$(nproc) KVER=\$kernelver KSRC=/lib/modules/\$kernelver/build"
CLEAN="'make' clean"
BUILT_MODULE_NAME[0]="$KMOD"
DEST_MODULE_LOCATION[0]="/updates/dkms"
AUTOINSTALL="YES"
DKMS
  else
    VER=$(grep PACKAGE_VERSION "$TMPD/$repo/dkms.conf" | head -1 | sed 's/.*="\(.*\)"/\1/')
    # Fix ARCH for arm64 in dkms build scripts
    if [ -f "$TMPD/$repo/dkms-make.sh" ]; then
      sed -i 's|^make |make ARCH=arm64 |' "$TMPD/$repo/dkms-make.sh"
    fi
  fi
  sudo cp -r "$TMPD/$repo" "/usr/src/${repo}-${VER}"
  sudo dkms add "${repo}/${VER}" 2>/dev/null || true
  # Build/install for NEW_KVER (the post-reboot kernel whose headers are present).
  # Building for the running kernel here fails — its headers are not installed.
  sudo dkms build "${repo}/${VER}" -k "$NEW_KVER" && sudo dkms install --force "${repo}/${VER}" -k "$NEW_KVER"
  rm -rf "$TMPD"
done

# WiFi USB auto-bind + hotspot-migrate scripts + modules auto-load.
# autobind binds the Realtek driver on hotplug; migrate moves the hotspot onto
# the USB WiFi (wlx*) when present and back to ap0 when removed. The autobind
# udev rule execs the migrate script, so BOTH must be installed — otherwise a
# hotplugged USB adapter is bound but the hotspot never moves off ap0.
install -m 755 "$DEPLOY_DIR/usr/local/bin/physicar-wifi-usb-autobind.sh" /usr/local/bin/physicar-wifi-usb-autobind.sh
install -m 755 "$DEPLOY_DIR/usr/local/bin/physicar-wifi-hotspot-migrate.sh" /usr/local/bin/physicar-wifi-hotspot-migrate.sh
ln -sf "$DEPLOY_DIR/etc/modules-load.d/physicar-wifi.conf" /etc/modules-load.d/physicar-wifi.conf

# noVNC symlink
[ -d /usr/share/novnc ] && ln -sf vnc_lite.html /usr/share/novnc/index.html

# Deploy openbox / tint2 config
mkdir -p /etc/xdg/openbox /home/physicar/.config/tint2

ln -sf "$DEPLOY_DIR/etc/xdg/openbox/rc.xml" /etc/xdg/openbox/rc.xml
ln -sf "$DEPLOY_DIR/home/physicar/.config/tint2/tint2rc" /home/physicar/.config/tint2/tint2rc
chown -R physicar:physicar /home/physicar/.config

ln -sf "$DEPLOY_DIR/usr/share/applications/xterm.desktop" /usr/share/applications/xterm.desktop

# ── ROS 2 Jazzy ──
# Download the apt signing key with retries + validation. A transient GitHub
# error (e.g. HTTP 400) would otherwise be saved verbatim as the "key", giving an
# invalid keyring -> apt reports the ROS repo as NOT signed -> ros-jazzy install
# fails -> `set -e` aborts the ENTIRE install (no services/build -> no kiosk).
_ros_key=/usr/share/keyrings/ros-archive-keyring.gpg
for _kt in 1 2 3 4 5 6; do
  curl -fsSL https://raw.githubusercontent.com/ros/rosdistro/master/ros.key -o "$_ros_key" 2>/dev/null || true
  [ "$(stat -c%s "$_ros_key" 2>/dev/null || echo 0)" -gt 1000 ] && break
  echo "  ros.key download failed (attempt $_kt/6), retrying in 5s..."
  sleep 5
done
if [ "$(stat -c%s "$_ros_key" 2>/dev/null || echo 0)" -le 1000 ]; then
  echo "ERROR: could not fetch a valid ROS apt key (network/GitHub issue). Re-run install."
  exit 1
fi
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/ros-archive-keyring.gpg] http://packages.ros.org/ros2/ubuntu $(lsb_release -cs) main" \
  | tee /etc/apt/sources.list.d/ros2.list > /dev/null
wait_for_apt
apt-get update -y
wait_for_apt
apt-get install -y \
  ros-jazzy-ros-base \
  ros-jazzy-rmw-cyclonedds-cpp \
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
  python3-pip \
  python3-dev \
  ros-jazzy-joy \
  ros-jazzy-camera-info-manager \
  ros-jazzy-rosbridge-server \
  ros-jazzy-ros2-control \
  ros-jazzy-ros2-controllers

# SLAM/Nav2 packages for host-side navigation practice
wait_for_apt
apt-get install -y --no-install-recommends \
  ros-jazzy-slam-toolbox \
  ros-jazzy-cartographer-ros \
  ros-jazzy-navigation2 \
  ros-jazzy-nav2-bringup \
  ros-jazzy-nav2-rviz-plugins \
  ros-jazzy-rqt-tf-tree \
  ros-jazzy-rqt-graph

set +u; source /opt/ros/jazzy/setup.bash; set -u
rosdep init 2>/dev/null || true
sudo -u physicar rosdep update --rosdistro jazzy 2>/dev/null || true

# Patch nav2 navigation_launch.py: disable docking_server & route_server
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
    # Non-composed Node blocks
    c = re.sub(
        r\"(            )(Node\\(\\n\\s+package='\" + pkg + r\"'.*?remappings=remappings,\\n\\s+\\),)\",
        lambda m: m.group(1) + '# ' + m.group(2).replace('\\n', '\\n' + m.group(1) + '# '),
        c, count=1, flags=re.DOTALL)
    # ComposableNode blocks
    c = re.sub(
        r\"(                    )(ComposableNode\\(\\n\\s+package='\" + pkg + r\"'.*?remappings=remappings,\\n\\s+\\),)\",
        lambda m: m.group(1) + '# ' + m.group(2).replace('\\n', '\\n' + m.group(1) + '# '),
        c, count=1, flags=re.DOTALL)
with open('$NAV2_LAUNCH', 'w') as f: f.write(c)
print('nav2 navigation_launch.py patched')
"
fi

# Prevent automatic package upgrades (lock installed versions)
apt-mark hold $(dpkg -l | grep -E '^ii  (ros-jazzy|gz-|libgz-)' | awk '{print $2}') 2>/dev/null || true
apt-mark hold linux-image-raspi linux-headers-raspi linux-image-generic linux-headers-generic 2>/dev/null || true

# python
runuser -u physicar -- python3 -m pip config set global.break-system-packages true 2>/dev/null || true

# Pin numpy<2 globally (cv_bridge C++ ABI requires numpy 1.x)
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
  smbus2 RPi.GPIO gpiozero adafruit-circuitpython-servokit \
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

  # libtensorflow_cc.so.2 has a NEEDED on the OpenMP runtime that the pip wheel
  # bundles under tensorflow.libs/ with a hashed soname (e.g. libomp-e9212f90.so.5).
  # Without it on the linker/loader path, physicar_deepracer fails to link
  # ("undefined reference to omp_*"), which leaves the package half-installed and
  # makes the whole device.launch.py abort ("package 'physicar_deepracer' not found").
  # Symlink the bundled libomp into /usr/local/lib so ld + ld.so resolve it.
  for _omp in "$(dirname "$TF_LIB")"/tensorflow.libs/libomp-*.so.*; do
    [ -f "$_omp" ] && ln -sf "$_omp" /usr/local/lib/"$(basename "$_omp")"
  done

  ldconfig
fi

# ── libcamera (build from source for RPi camera support) ──
if ! pkg-config --exists libcamera 2>/dev/null; then
  TMPD=$(mktemp -d)
  git clone https://github.com/raspberrypi/libcamera.git --depth 1 "$TMPD/libcamera"
  cd "$TMPD/libcamera" && meson setup build --buildtype=release \
    -Dpipelines=rpi/pisp,rpi/vc4 \
    -Dipas=rpi/pisp,rpi/vc4 \
    -Dpycamera=disabled \
    -Dtest=false \
    -Dcam=disabled \
    -Dqcam=disabled \
    -Dgstreamer=disabled \
    -Ddocumentation=disabled
  ninja -C build -j2
  ninja -C build install
  echo '/usr/local/lib/aarch64-linux-gnu' > /etc/ld.so.conf.d/libcamera.conf
  ldconfig
  rm -rf "$TMPD"
  cd /
fi

# ── Chromium kiosk ──
fc-cache -fv 2>/dev/null || true
wait_for_apt
apt-get install -y chromium-browser
snap connect chromium:desktop :desktop 2>/dev/null || true
snap connect chromium:desktop-legacy :desktop-legacy 2>/dev/null || true

mkdir -p /etc/chromium/policies/managed /var/snap/chromium/current/policies/managed

ln -sf "$DEPLOY_DIR/etc/chromium/policies/managed/kiosk-policy.json" /etc/chromium/policies/managed/kiosk-policy.json

cp /etc/chromium/policies/managed/kiosk-policy.json /var/snap/chromium/current/policies/managed/

# ┌─────────────────────────────────────────────────────────────────────────────┐
# │  3. Network Configuration                                                  │
# └─────────────────────────────────────────────────────────────────────────────┘

echo "[3/7] Network configuration..."

# ── NetworkManager ──
# Guard: only configure if NetworkManager was installed
if ! command -v nmcli &>/dev/null; then
  echo "ERROR: NetworkManager not installed. Section 2 (Package Installation) likely failed."
  exit 1
fi

mkdir -p /etc/NetworkManager/conf.d
ln -sf "$DEPLOY_DIR/etc/NetworkManager/conf.d/10-globally-managed-devices.conf" /etc/NetworkManager/conf.d/10-globally-managed-devices.conf

mkdir -p /etc/NetworkManager/dispatcher.d

cat > /etc/NetworkManager/dispatcher.d/90-physicar-cert <<'__NM_CERT__'
#!/usr/bin/env bash
# NetworkManager dispatcher: poke the physicar.sh cert fetcher whenever a
# network interface comes up.

iface="$1"
action="$2"

case "$action" in
    up|dhcp4-change|dhcp6-change|connectivity-change) ;;
    *) exit 0 ;;
esac
[ "$iface" = "ap0" ] && exit 0
[ "$iface" = "lo" ]  && exit 0

PID_FILE="/run/physicar/cert-fetcher.pid"
[ -r "$PID_FILE" ] || exit 0
pid=$(cat "$PID_FILE" 2>/dev/null)
[ -n "$pid" ] || exit 0
kill -0 "$pid" 2>/dev/null && kill -USR1 "$pid" 2>/dev/null
exit 0
__NM_CERT__
chmod 0755 /etc/NetworkManager/dispatcher.d/90-physicar-cert

systemctl enable NetworkManager
systemctl start NetworkManager
sleep 3

# ── Migrate imager Wi-Fi credentials into NetworkManager ──
# The stock image's Wi-Fi (from Raspberry Pi Imager) lives in cloud-init's
# netplan as a networkd/wpa_supplicant config. Once networkd is retired
# below, that connection would vanish and the device goes hotspot-only
# ("Wi-Fi stopped working after install"). Recreate each SSID/PSK found
# there as an NM profile so the same network reconnects under NM.
if [ -f /etc/netplan/50-cloud-init.yaml ]; then
python3 - <<'__MIGRATE_WIFI__'
import subprocess, yaml
cfg = yaml.safe_load(open('/etc/netplan/50-cloud-init.yaml')) or {}
aps = ((cfg.get('network', {}).get('wifis', {}) or {}).get('wlan0', {}) or {}).get('access-points', {}) or {}
have = subprocess.run(['nmcli', '-t', '-f', 'NAME', 'connection', 'show'],
                      capture_output=True, text=True).stdout.splitlines()
for ssid, opts in aps.items():
    if ssid in have:
        continue
    psk = None
    if isinstance(opts, dict):
        psk = opts.get('password') or (opts.get('auth') or {}).get('password')
    cmd = ['nmcli', 'connection', 'add', 'type', 'wifi', 'con-name', ssid,
           'ifname', 'wlan0', 'ssid', ssid, 'connection.autoconnect', 'yes']
    if psk:
        cmd += ['wifi-sec.key-mgmt', 'wpa-psk', 'wifi-sec.psk', str(psk)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    print('  migrated Wi-Fi profile:', ssid, r.returncode, (r.stdout or r.stderr).strip())
__MIGRATE_WIFI__
fi

# ── Switch to netplan ──
[ -f /etc/netplan/50-cloud-init.yaml ] && cp /etc/netplan/50-cloud-init.yaml /etc/netplan/50-cloud-init.yaml.bak

# Stop cloud-init regenerating the deleted netplan on every boot. Without
# this the networkd configs come back at next boot and fight NetworkManager
# for wlan0 — two supplicants on one radio, connect/disconnect loops on the
# hotspot and STA ("unstable, keeps dropping").
mkdir -p /etc/cloud/cloud.cfg.d
echo 'network: {config: disabled}' > /etc/cloud/cloud.cfg.d/99-disable-network-config.cfg

ln -sf "$DEPLOY_DIR/etc/netplan/01-netcfg.yaml" /etc/netplan/01-netcfg.yaml

rm -f /etc/netplan/50-cloud-init.yaml 2>/dev/null || true
netplan generate 2>/dev/null || true
netplan apply 2>/dev/null || true
sleep 2

# ── Retire systemd-networkd — one manager per interface ──
# NetworkManager owns everything from here (netplan renderer above).
# Leaving networkd enabled produces the dual-manager instability.
systemctl stop 'netplan-wpa-*' 2>/dev/null || true
systemctl disable --now systemd-networkd.service systemd-networkd.socket \
  systemd-networkd-wait-online.service 2>/dev/null || true
nmcli device connect wlan0 2>/dev/null || true

# ┌─────────────────────────────────────────────────────────────────────────────┐
# │  4. Nginx                                                                  │
# └─────────────────────────────────────────────────────────────────────────────┘

echo "[4/7] Nginx setup..."

rm -f /etc/nginx/sites-enabled/default
mkdir -p /etc/nginx/ssl /etc/nginx/conf.d /etc/nginx/html

# Generate self-signed SSL certificate
if [ ! -f /etc/nginx/ssl/physicar.crt ]; then
  openssl req -x509 -nodes -days 3650 -newkey rsa:2048 \
    -keyout /etc/nginx/ssl/physicar.key \
    -out /etc/nginx/ssl/physicar.crt \
    -subj "/CN=physicar.local/O=PhysiCar" \
    -addext "subjectAltName=DNS:physicar.local,DNS:localhost,IP:127.0.0.1" 2>/dev/null
fi

if [ ! -f /etc/nginx/ssl/le.crt ]; then
  cp /etc/nginx/ssl/physicar.crt /etc/nginx/ssl/le.crt
  cp /etc/nginx/ssl/physicar.key /etc/nginx/ssl/le.key
  chmod 600 /etc/nginx/ssl/le.key
fi

echo '# populated at boot' | tee /etc/nginx/conf.d/physicar_password.map >/dev/null
echo '# populated at boot' | tee /etc/nginx/conf.d/physicar_session.map >/dev/null
echo 'pre-boot' | tee /etc/nginx/html/boot_token >/dev/null

ln -sf "$DEPLOY_DIR/etc/nginx/sites-available/physicar" /etc/nginx/sites-available/physicar

ln -sf /etc/nginx/sites-available/physicar /etc/nginx/sites-enabled/physicar
systemctl enable nginx

# ┌─────────────────────────────────────────────────────────────────────────────┐
# │  5. Firewall                                                               │
# └─────────────────────────────────────────────────────────────────────────────┘

echo "[5/7] Firewall setup..."

ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw allow 5353/udp           # mDNS
ufw allow 7400:7500/udp      # ROS 2 DDS Discovery
ufw allow 7400:7500/tcp      # ROS 2 DDS Data
ufw allow in on ap0          # WiFi Hotspot
ufw route allow in on ap0 out on wlan0
ufw --force enable

# ┌─────────────────────────────────────────────────────────────────────────────┐
# │  6. Services                                                               │
# └─────────────────────────────────────────────────────────────────────────────┘

echo "[6/7] Service setup..."

systemctl enable --now avahi-daemon

# code-server
if [ ! -f /usr/local/bin/code-server ]; then
  curl -fsSL https://code-server.dev/install.sh | sh -s -- --method=standalone --prefix=/usr/local
fi
# 'code' command (so terminal users can do: code file.sh)
ln -sf /usr/local/bin/code-server /usr/local/bin/code

# ── Webview microphone/camera patch ──
# VS Code's webview iframes don't delegate mic/cam permission, which blocks
# getUserMedia in every webview below them (extension panels, app.physicar).
# Append 'microphone; camera' to the webview iframe allow-list in the served
# workbench bundle. Idempotent; re-run at every boot too (physicar.sh) since
# a code-server update restores the bundle. Requires a secure context (https
# or localhost) on the browser side — nginx already serves https.
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

# nginx (www-data) needs to traverse /home/physicar to serve static files
# (studio.html, login.html, etc.). Ubuntu defaults home dirs to 750.
# Add www-data to physicar group so nginx can traverse without opening to all users.
usermod -aG physicar www-data

# Hardware access for the physicar user (physicar.service runs as User=physicar):
#  - gpio  : /sys/class/pwm/pwmchip0/* — physicar.sh chgrps the PWM sysfs nodes
#            to 'gpio'; without the group existing AND physicar being a member,
#            the chgrp fails and the nodes stay root:root, so physicar_driver
#            cannot write duty_cycle → steering/ESC dead → wheels don't move.
#  - video : /dev/media* and /dev/video* (libcamera/V4L2) — without it the
#            physicar_camera node hits "Failed to open media device: Permission
#            denied" and crash-loops, so no camera stream.
#  - render: /dev/dri/render* (GPU, used by libcamera/VirtualGL).
#  - audio : /dev/snd/* — without it mpv playback fails ("Permission
#            denied"), so no /audio playback / intro sound.
#  - dialout: /dev/ttyUSB* serial (lidar / expansion board), in case udev
#            MODE=0666 is ever tightened.
#  - i2c   : /dev/i2c-* (servo / expansion board over I2C).
#  - input : /dev/input/event* and /dev/input/js* (Bluetooth gamepad) — without
#            it joy_node can't read the controller, so /joy never publishes and
#            the kiosk "Joystick: Enable" toggle does nothing.
groupadd -f gpio
for _grp in gpio video render audio dialout i2c plugdev input; do
  getent group "$_grp" >/dev/null && usermod -aG "$_grp" physicar
done

# code-server branding: copy icons from physicar-ros
CS_RES=$(find /usr/local/lib -path '*/code-server-*/lib/vscode/resources/server' -type d 2>/dev/null | head -1)
if [ -n "$CS_RES" ]; then
  cp "$PHYSICAR_ROS_DIR/physicar_webserver/static/favicon.ico" "$CS_RES/favicon.ico"
  cp "$PHYSICAR_ROS_DIR/physicar_webserver/static/img/code-192.png" "$CS_RES/code-192.png"
  cp "$PHYSICAR_ROS_DIR/physicar_webserver/static/img/code-512.png" "$CS_RES/code-512.png"
fi

# code-server 4.12x serves the browser tab icon from src/browser/media
# (workbench.html links favicon-dark-support.svg first, then favicon.ico) —
# the resources/server copies above never reach the tab. Replace the ico,
# both SVGs (as an SVG-wrapped PNG), and the PWA icons.
CS_MEDIA=$(find /usr/local/lib -path '*/code-server-*/src/browser/media' -type d 2>/dev/null | head -1)
if [ -n "$CS_MEDIA" ]; then
  cp "$PHYSICAR_ROS_DIR/physicar_webserver/static/favicon.ico" "$CS_MEDIA/favicon.ico"
  _B64=$(base64 -w0 "$PHYSICAR_ROS_DIR/physicar_webserver/static/img/code-192.png")
  for _svg in favicon.svg favicon-dark-support.svg; do
    printf '<svg xmlns="http://www.w3.org/2000/svg" width="192" height="192"><image width="192" height="192" href="data:image/png;base64,%s"/></svg>' "$_B64" > "$CS_MEDIA/$_svg"
  done
  for _png in pwa-icon-192.png pwa-icon-maskable-192.png; do
    [ -f "$CS_MEDIA/$_png" ] && cp "$PHYSICAR_ROS_DIR/physicar_webserver/static/img/code-192.png" "$CS_MEDIA/$_png"
  done
  for _png in pwa-icon-512.png pwa-icon-maskable-512.png; do
    [ -f "$CS_MEDIA/$_png" ] && cp "$PHYSICAR_ROS_DIR/physicar_webserver/static/img/code-512.png" "$CS_MEDIA/$_png"
  done
fi

# Workbench titlebar icon (.window-appicon) — CSS points at out/media/code-icon.svg
CS_OUT_MEDIA=$(find /usr/local/lib -path '*/code-server-*/lib/vscode/out/media' -type d 2>/dev/null | head -1)
if [ -n "$CS_OUT_MEDIA" ] && [ -f "$CS_OUT_MEDIA/code-icon.svg" ]; then
  _B64=$(base64 -w0 "$PHYSICAR_ROS_DIR/physicar_webserver/static/img/code-192.png")
  printf '<svg xmlns="http://www.w3.org/2000/svg" width="192" height="192"><image width="192" height="192" href="data:image/png;base64,%s"/></svg>' "$_B64" > "$CS_OUT_MEDIA/code-icon.svg"
fi

# code-server user settings
CS_USER_DIR="/home/physicar/.local/share/code-server/User"
sudo -u physicar mkdir -p "$CS_USER_DIR"
ln -sf "$DEPLOY_DIR/home/physicar/.local/share/code-server/User/settings.json" "$CS_USER_DIR/settings.json"

# Install boot script (from repo)
chmod +x "$DEPLOY_DIR/physicar.sh"

# sudoers for physicar user (full NOPASSWD access)
echo 'physicar ALL=(ALL) NOPASSWD: ALL' > /etc/sudoers.d/physicar
chmod 440 /etc/sudoers.d/physicar

# systemd services
ln -sf "$DEPLOY_DIR/etc/systemd/system/physicar.service" /etc/systemd/system/physicar.service
ln -sf "$DEPLOY_DIR/etc/systemd/system/physicar-code.service" /etc/systemd/system/physicar-code.service
ln -sf "$DEPLOY_DIR/etc/systemd/system/physicar-myapp.service" /etc/systemd/system/physicar-myapp.service

# /opt/physicar symlink (services reference this path)
if [[ "$PHYSICAR_WS" != "/opt/physicar" ]]; then
  ln -sfn "$PHYSICAR_WS" /opt/physicar
fi

chown -R physicar:physicar "$PHYSICAR_WS"

# Runtime data dir + log files (physicar-owned).
# systemd sets up StandardOutput=append:... BEFORE ExecStart runs, so the
# target dir/file must already exist and be writable by the physicar user —
# otherwise physicar.service fails with 209/STDOUT before physicar.sh ever runs.
# Pre-creating the log as physicar also lets the physicar-user ExecStartPre
# truncate it (systemd would otherwise create it root-owned on first boot).
sudo -u physicar mkdir -p /opt/physicar/userdata
sudo -u physicar touch /opt/physicar/userdata/physicar.log

# ── Seed ~/.bashrc for physicar user ──
# Source the repo file directly (not a copy): env changes shipped in future
# updates take effect on the next shell without re-running install.
grep -qF "deploy/device/bashrc-append" /home/physicar/.bashrc 2>/dev/null || \
  sudo -u physicar tee -a /home/physicar/.bashrc > /dev/null <<'__BASHRC_HOOK__'

# physicar-ros environment
. /opt/physicar/src/physicar-ros/deploy/device/bashrc-append
__BASHRC_HOOK__

# echo "DEV=true" | tee /opt/physicar/userdata/.env

systemctl daemon-reload
systemctl enable physicar.service
systemctl enable physicar-code.service
systemctl enable physicar-myapp.service

# Initial ROS 2 workspace build
sudo -u physicar bash -c 'set +u; source /opt/ros/jazzy/setup.bash; set -u; cd '"$PHYSICAR_WS"' && colcon build --symlink-install'
rm -rf "$PHYSICAR_WS/log"

# ┌─────────────────────────────────────────────────────────────────────────────┐
# │  7. Cleanup (remove caches, logs, install artifacts)                       │
# └─────────────────────────────────────────────────────────────────────────────┘

echo "[7/7] Cleanup..."

# Remove old kernel packages (keep only the newest)
for pkg in $(dpkg -l | grep '^ii  linux-\(image\|headers\|modules\)-[0-9]' | awk '{print $2}' | sort -V); do
  case "$pkg" in
    *"${NEW_KVER}"*) continue ;;
    *) apt-get purge -y "$pkg" 2>/dev/null || true ;;
  esac
done

journalctl --vacuum-size=1M 2>/dev/null || true
rm -rf /var/log/apt/*
rm -f /var/log/dpkg.log*
rm -f /var/log/alternatives.log*
: > /var/log/syslog
: > /var/log/auth.log

rm -f /root/.bash_history /home/physicar/.bash_history

echo ""
echo "=========================================="
echo "      Physicar Host Setup Complete       "
echo "=========================================="
echo ""
