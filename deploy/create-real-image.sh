#!/usr/bin/env bash
set -euo pipefail

# Defaults
SOURCE="/dev/mmcblk0"
USB_DISK=""
OUTPUT_NAME="physicar-$(date +%Y%m%d).img"
COMPRESS=true

log()   { echo "[INFO] $1"; }
warn()  { echo "[WARN] $1"; }
error() { echo "[ERROR] $1"; exit 1; }

[[ $EUID -ne 0 ]] && error "This script must be run as root: sudo bash $0"

trap 'umount /mnt/physicar_usb 2>/dev/null || true' EXIT

log "PhysiCar SD Card Image Creator"

# Step 1: Find USB disk
disks=$(lsblk -dpno NAME,SIZE,TYPE | awk '$3=="disk" && $2!="0B" && $1!~/mmcblk|loop/' | awk '{print $1}')

if [[ -z "$disks" ]]; then
    error "No USB disk found. Please connect a USB SD card reader with a card inserted."
fi

count=$(echo "$disks" | wc -l)

if [[ $count -gt 1 ]]; then
    echo "Multiple USB disks found:"
    lsblk -dpno NAME,SIZE,MODEL | grep -F "$(echo "$disks")"
    echo ""
    read -rp "Enter the device path to use: " USB_DISK
else
    USB_DISK="$disks"
fi

[[ ! -b "$USB_DISK" ]] && error "Device $USB_DISK does not exist."

# Size check: USB peak usage = max(full source size [dd], used data x1.6 [shrunk .img + .gz]).
src_size=$(blockdev --getsize64 "$SOURCE")
usb_size=$(blockdev --getsize64 "$USB_DISK")

used_root=$(df -B1 --output=used / | tail -1 | tr -d ' ')
used_boot=$(df -B1 --output=used /boot/firmware 2>/dev/null | tail -1 | tr -d ' ' || echo 0)
used_size=$(( used_root + used_boot ))
required_used=$(( used_size * 8 / 5 ))
required=$(( src_size > required_used ? src_size : required_used ))

if [[ $usb_size -lt $required ]]; then
    error "USB disk ($USB_DISK: $((usb_size/1024/1024/1024))G) too small. Need >= $((required/1024/1024/1024))G (source $((src_size/1024/1024/1024))G, used data $((used_size/1024/1024/1024))G x1.6)."
fi

log "Source: $SOURCE ($(lsblk -dno SIZE "$SOURCE"))"
log "USB:    $USB_DISK ($(lsblk -dno SIZE "$USB_DISK")) [$(lsblk -dno MODEL "$USB_DISK")]"

warn "This will OVERWRITE all data on $USB_DISK!"
read -rp "Type 'yes' to continue: " confirm
[[ "$confirm" != "yes" ]] && error "Aborted by user."

# Step 2: Format USB as exFAT (Windows/macOS/Linux compatible)
log "Formatting $USB_DISK as exFAT..."
for part in "${USB_DISK}"*[0-9]; do
    umount "$part" 2>/dev/null || true
done
wipefs -a "$USB_DISK"
parted -s "$USB_DISK" mklabel msdos mkpart primary fat32 1MiB 100%
partprobe "$USB_DISK"
sleep 2

usb_part="${USB_DISK}1"
[[ ! -b "$usb_part" ]] && usb_part="${USB_DISK}p1"
[[ ! -b "$usb_part" ]] && error "Cannot find partition on $USB_DISK after format."

mkfs.exfat -n "PHYSICAR" "$usb_part"

mount_point="/mnt/physicar_usb"
mkdir -p "$mount_point"
mount "$usb_part" "$mount_point"

# Step 3: Disable swap and zero swapfile (saves ~8GB)
if swapon --show | grep -q '/swapfile'; then
    log "Disabling swap and zeroing swapfile..."
    swapoff /swapfile
    swap_mb=$(( $(stat -c%s /swapfile) / 1024 / 1024 ))
    dd if=/dev/zero of=/swapfile bs=1M count=$swap_mb 2>/dev/null || true
    mkswap /swapfile
fi

# Step 4: Zero free blocks for better compression
log "Zeroing free blocks (fstrim)..."
fstrim -v / 2>/dev/null || warn "fstrim on / failed (non-fatal)"
fstrim -v /boot/firmware 2>/dev/null || true

# Step 5: dd source -> .img file on USB
img_path="$mount_point/${OUTPUT_NAME}"
src_bytes=$(blockdev --getsize64 "$SOURCE")
src_size_g=$(( src_bytes / 1024 / 1024 / 1024 ))

log "Copying $SOURCE -> $img_path (${src_size_g}G)..."
pv -petras "$src_bytes" < "$SOURCE" | dd of="$img_path" bs=4M conv=fsync iflag=fullblock 2>/dev/null
sync

# Step 6: PiShrink (shrink filesystem + partition + add first-boot auto-expand)
log "Running PiShrink (shrink + first-boot auto-expand)..."
pishrink.sh -a "$img_path" || warn "PiShrink failed (non-fatal, image is still valid)"

img_final_mb=$(( $(stat -c%s "$img_path") / 1024 / 1024 ))
log "Image size after PiShrink: ${img_final_mb}MB"

# Step 7: Compress (optional)
if [[ "$COMPRESS" == true ]]; then
    img_bytes_after=$(stat -c%s "$img_path")
    log "Compressing with pigz..."
    pv -petras "$img_bytes_after" "$img_path" | pigz -1 > "${img_path}.gz"
    sync
    rm -f "$img_path"
    final_name="${OUTPUT_NAME}.gz"
else
    final_name="${OUTPUT_NAME}"
fi

final_size=$(du -h "$mount_point/$final_name" | awk '{print $1}')
umount "$mount_point"

log "DONE!"
log "Image: $final_name ($final_size)"
log "Stored on: $USB_DISK (label: PHYSICAR)"

log "To flash this image:"
if [[ "$COMPRESS" == true ]]; then
    log "  - Raspberry Pi Imager -> Use custom -> select the .img.gz file"
    log "  - Or: zcat ${OUTPUT_NAME}.gz | sudo dd of=/dev/sdX bs=4M status=progress"
else
    log "  - Raspberry Pi Imager -> Use custom -> select the .img file"
    log "  - Or: sudo dd if=${OUTPUT_NAME} of=/dev/sdX bs=4M status=progress"
fi
log "The image will auto-expand to fill the target SD card on first boot."
