# PhysiCar Deploy

## SIM

Ubuntu 24.04 — no Docker, ROS nodes run natively.
Works on Codespaces, local VM, cloud instance, etc.

### Install

```bash
sudo mkdir -p /opt/physicar/src && sudo chown -R physicar:physicar /opt/physicar
git clone https://github.com/physicar-ai/physicar-ros.git /opt/physicar/src/physicar-ros
git clone https://github.com/physicar-ai/physicar-sim.git /opt/physicar/src/physicar-sim
sudo bash /opt/physicar/src/physicar-ros/deploy/install-sim.sh
```

## Real (Raspberry Pi 5)

### Prerequisites

1. Flash Ubuntu 24.04 Server image to SD card with [Raspberry Pi Imager](https://www.raspberrypi.com/software/) (set username to `physicar`)
2. Connect Ethernet cable
3. Boot and SSH in: `ssh physicar@physicar.local`

### Install

```bash
sudo apt-get update && sudo apt-get install -y git
sudo mkdir -p /opt/physicar/src && sudo chown -R physicar:physicar /opt/physicar
sudo -u physicar git clone https://github.com/physicar-ai/physicar-ros.git /opt/physicar/src/physicar-ros
sudo bash /opt/physicar/src/physicar-ros/deploy/install-real.sh
sudo reboot
```

### Create a distributable SD image

Clone a set-up robot to a flashable image. Run on the robot with a USB drive
plugged in (wiped to exFAT, holds the output; needs `>= max(SD size, used x1.6)`).

```bash
sudo bash /opt/physicar/src/physicar-ros/deploy/create-real-image.sh
```

Output: `physicar-YYYYMMDD.img.gz` on the USB. Flash with Raspberry Pi Imager; auto-expands on first boot.



## Update propagation (real robots)

`updater.sh` refreshes the repo checkout and rebuilds ROS packages; it never
re-runs the installer. For a change to reach robots in the field it must
therefore live on one of these paths:

1. **Repo-direct (preferred)** — the running system executes/serves the repo
   copy itself: `physicar.sh`, systemd units, udev rules, nginx config,
   netplan/NM conf, webserver static+python, the wifi autobind/migrate
   scripts (udev runs them from the repo), `cyclonedds.xml`, bashrc hook.
2. **Boot-refreshed** — `physicar.sh` re-applies it every boot when the repo
   copy changed: snap chromium kiosk policy, NM dispatcher hook (root-owned
   file requirement), the PWM overlay dtbo (recompiled; applies next boot),
   code-server binary (pinned by `deploy/code-server-version`), code-server
   webview patches and settings merge, DDS sysctl values.
3. **Install-only — NOT updatable in the field.** Changing any of these
   requires a new SD image: apt/pip package set (except the `physicar`
   package, which the updater upgrades), ufw rules, `config.txt` lines,
   sudoers, group memberships, usb_modeswitch config, DKMS drivers, kernel.

`deploy/real/**` is never read by the simulator; sim-shared surfaces are
`physicar_webserver/`, `physicar_tools/`, the ROS packages, `updater.sh`,
`cyclonedds.xml`, and `deploy/sim/**` + `install-sim.sh`.
