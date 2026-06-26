# Device Password Reset

How to set a new login password on a Device (Raspberry Pi 5) via its SD card when
you don't know the current one. (The password is derived from the board serial, so
it can't be read off the card — only reset.)

**You need**: an SD card reader. You only edit the **`system-boot`** partition (FAT32);
the ext4 root partition is not needed.

## Steps

1. Power off, remove the SD card, insert it into a computer, and open the
   **`system-boot`** partition.

2. Append to **`user-data`** (`CHANGE_ME` = your new password, **8–63 ASCII chars**):

   ```yaml
   chpasswd:
     expire: false
     users:
       - name: physicar
         password: "CHANGE_ME"
         type: text
   bootcmd:
     - [ sh, -c, "install -d -o physicar -g physicar /opt/physicar/userdata; printf 'CHANGE_ME' > /opt/physicar/userdata/password; chown physicar:physicar /opt/physicar/userdata/password; date > /boot/firmware/recover-ran.txt 2>/dev/null || true" ]
   ```

3. **`meta-data`** — change `instance-id` to any new value:

   ```
   instance-id: recovery-1
   ```

4. **`cmdline.txt`** — change the `i=` value on the single line to match (leave the
   rest untouched):

   ```
   ... ds=nocloud;i=recovery-1
   ```

5. Eject, put the card back in the device, and boot. After **1–2 min**, log in with
   the new password:

   ```bash
   ssh physicar@<device-ip>
   ```

   The hotspot (`physicar-XXXX`) password becomes the same value.

## Verify / revert

- **If it didn't work**: re-open `system-boot` and check for `recover-ran.txt`.
  Present → it ran (check password/typos); absent → `instance-id` wasn't changed in
  both files.
- **Back to the auto-generated password**: `sudo rm -f /opt/physicar/userdata/password && sudo reboot`
  (and remove the blocks you added to `user-data`).
