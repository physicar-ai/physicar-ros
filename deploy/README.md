# PhysiCar Device Setup

Raspberry Pi 5 + Ubuntu 24.04 (Noble) arm64

## Prerequisites

1. Flash Ubuntu 24.04 Server image to SD card with Raspberry Pi Imager (set username to `physicar`)
2. Connect Ethernet cable
3. Boot and SSH in: `ssh physicar@physicar.local`

## Install

```bash
sudo apt-get update && sudo apt-get install -y git
sudo -u physicar mkdir -p /home/physicar/physicar_ws/src
sudo -u physicar git clone https://github.com/physicar-ai/physicar-ros.git /home/physicar/physicar_ws/src/physicar-ros
sudo bash /home/physicar/physicar_ws/src/physicar-ros/deploy/install-device.sh
sudo reboot
```
