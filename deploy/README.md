# PhysiCar Deploy

## Device (Raspberry Pi 5)

Raspberry Pi 5 + Ubuntu 24.04 (Noble) arm64

### Prerequisites

1. Flash Ubuntu 24.04 Server image to SD card with Raspberry Pi Imager (set username to `physicar`)
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

### Directory Layout

```
/opt/physicar/
├── src/
│   ├── physicar-ros/    # ROS2 packages + deploy/
│   └── physicar-sim/    # Gazebo worlds, sim_api, models
├── build/               # colcon build output
├── install/             # colcon install (symlink-install)
├── myapp/               # student myapp (run.sh, log)
└── .env                 # SIM=true
```
