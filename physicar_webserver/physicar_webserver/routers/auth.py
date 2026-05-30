#!/usr/bin/env python3
"""
Auth router — only exposes the device-password change endpoint.

All session/login/token authentication is handled by nginx
(see deploy/device/etc/nginx/).
"""

import os
import subprocess

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from physicar_webserver.auth import clear_password_cache


router = APIRouter()


class ChangePasswordRequest(BaseModel):
    new_password: str


@router.post("/auth/password")
async def change_password(req: ChangePasswordRequest):
    """
    Change the device password (rewrites /opt/physicar/userdata/password and reboots).

    The user is already authenticated by nginx to reach this endpoint, so we
    don't ask for the current password. After a reboot physicar.sh
    regenerates nginx auth maps from the new file, so SSH/AP/web all converge.

    Validation: 8-63 chars, ASCII printable, no whitespace.
    """
    from physicar_webserver.sim import reject_in_sim
    reject_in_sim("change device password")

    new_pw = req.new_password or ""
    if not (8 <= len(new_pw) <= 63):
        raise HTTPException(status_code=400, detail="Password must be 8-63 characters.")
    if any(c.isspace() for c in new_pw):
        raise HTTPException(status_code=400, detail="Password cannot contain spaces or whitespace.")
    if not all(0x21 <= ord(c) <= 0x7E for c in new_pw):
        raise HTTPException(status_code=400, detail="Password contains invalid characters.")

    try:
        os.makedirs("/opt/physicar/userdata", exist_ok=True)
        with open("/opt/physicar/userdata/password", "w") as f:
            f.write(new_pw + "\n")
        os.chmod("/opt/physicar/userdata/password", 0o600)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save password: {e}")

    clear_password_cache()

    ok, msg = _schedule_reboot()
    if not ok:
        return {
            "success": True,
            "rebooting": False,
            "message": f"Password saved, but reboot failed: {msg}. Please reboot manually.",
        }
    return {"success": True, "rebooting": True, "message": "Password changed. Rebooting…"}


def _schedule_reboot() -> tuple[bool, str]:
    """Schedule a host reboot. Returns (ok, message)."""
    try:
        subprocess.Popen(
            ["sh", "-c", "(sleep 2 && sudo reboot) >/dev/null 2>&1 &"],
            start_new_session=True,
        )
        return True, "rebooting"
    except Exception as e:  # noqa: BLE001
        return False, str(e)


@router.delete("/auth/password")
async def reset_password():
    """
    Reset the device password to its built-in default.

    Removes /opt/physicar/userdata/password and reboots the host. After reboot
    physicar.sh recomputes the password from the serial-number hash
    (production) or falls back to "physicar" (DEV/SIM), and rewrites the
    nginx auth maps so SSH/AP/web all converge on the new value.
    """
    from physicar_webserver.sim import reject_in_sim
    reject_in_sim("reset device password")

    pw_file = "/opt/physicar/userdata/password"
    try:
        if os.path.isfile(pw_file):
            os.remove(pw_file)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Failed to remove {pw_file}: {e}")

    clear_password_cache()

    ok, msg = _schedule_reboot()
    if not ok:
        return {
            "success": True,
            "rebooting": False,
            "message": f"Password reset, but reboot failed: {msg}. Please reboot manually.",
        }
    return {"success": True, "rebooting": True, "message": "Password reset. Rebooting…"}
