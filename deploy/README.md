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

## Device (Raspberry Pi 5)

### Prerequisites

1. Flash Ubuntu 24.04 Server image to SD card with [Raspberry Pi Imager](https://www.raspberrypi.com/software/) (set username to `physicar`)
2. Connect Ethernet cable
3. Boot and SSH in: `ssh physicar@physicar.local`

### Install

```bash
sudo apt-get update && sudo apt-get install -y git
sudo mkdir -p /opt/physicar/src && sudo chown -R physicar:physicar /opt/physicar
sudo -u physicar git clone https://github.com/physicar-ai/physicar-ros.git /opt/physicar/src/physicar-ros
sudo bash /opt/physicar/src/physicar-ros/deploy/install-device.sh
sudo reboot
```

### Create a distributable SD image

Clone a set-up device to a flashable image. Run on the device with a USB drive
plugged in (wiped to exFAT, holds the output; needs `>= max(SD size, used x1.6)`).

```bash
sudo bash /opt/physicar/src/physicar-ros/deploy/create-device-image.sh
```

Output: `physicar-YYYYMMDD.img.gz` on the USB. Flash with Raspberry Pi Imager; auto-expands on first boot.


