#!/usr/bin/env python3
"""
Bluetooth router — BlueZ control via bluetoothctl.

Endpoints are mounted at /network/bluetooth/* to live alongside the
existing network surface.
"""

import asyncio
import os
import re
import subprocess
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from physicar_webserver.sim import is_sim_mode

router = APIRouter(prefix="/network/bluetooth", tags=["Bluetooth"])


def _bump():
    """Wake the network SSE broadcaster so all tabs see the change."""
    try:
        from physicar_webserver.routers.network import _bump_network
        _bump_network()
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Models
# ─────────────────────────────────────────────────────────────────────────────

class BtAdapter(BaseModel):
    available: bool
    powered: bool
    discoverable: bool
    discovering: bool
    name: str = ""
    address: str = ""


class BtDevice(BaseModel):
    mac: str
    name: str
    paired: bool = False
    trusted: bool = False
    connected: bool = False
    rssi: Optional[int] = None
    icon: str = ""


class BtMacRequest(BaseModel):
    mac: str


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

_MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$")


def _validate_mac(mac: str) -> str:
    """Validate and normalise a MAC address to upper-case colon form."""
    if not mac or not _MAC_RE.match(mac):
        raise HTTPException(status_code=400, detail="Invalid MAC address")
    return mac.upper()


def _run(cmd: list[str], timeout: float = 10.0) -> subprocess.CompletedProcess:
    """Run `cmd` and capture output."""
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _bctl(args: str, timeout: float = 10.0) -> str:
    """Run a bluetoothctl command line and return stdout (best-effort)."""
    try:
        proc = _run(["bluetoothctl"] + args.split(), timeout=timeout)
        return (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        return ""
    except FileNotFoundError:
        return ""


def _bctl_script(commands: list[str], timeout: float = 30.0) -> str:
    """Run multiple bluetoothctl commands sequentially and join their output.

    bt-agent runs as a separate daemon (started by physicar.sh), so
    passkey/confirm prompts are auto-accepted.
    """
    parts: list[str] = []
    for cmd in commands:
        parts.append(_bctl(cmd, timeout=timeout))
    return "\n".join(parts)


def _has_bluetoothctl() -> bool:
    try:
        proc = _run(["which", "bluetoothctl"], timeout=3)
        return proc.returncode == 0 and bool(proc.stdout.strip())
    except Exception:
        return False


def _parse_show(text: str) -> BtAdapter:
    """Parse `bluetoothctl show` output into a BtAdapter."""
    if not text.strip():
        return BtAdapter(available=False, powered=False, discoverable=False, discovering=False)
    name = ""
    addr = ""
    powered = False
    discoverable = False
    discovering = False
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("Controller "):
            parts = line.split()
            if len(parts) >= 2:
                addr = parts[1]
        elif line.startswith("Name:"):
            name = line.split(":", 1)[1].strip()
        elif line.startswith("Powered:"):
            powered = "yes" in line.lower()
        elif line.startswith("Discoverable:"):
            discoverable = "yes" in line.lower()
        elif line.startswith("Discovering:"):
            discovering = "yes" in line.lower()
    return BtAdapter(
        available=bool(addr),
        powered=powered,
        discoverable=discoverable,
        discovering=discovering,
        name=name,
        address=addr,
    )


def _parse_devices(text: str) -> list[BtDevice]:
    """Parse `bluetoothctl devices` output → list of (mac, name)."""
    out: list[BtDevice] = []
    for line in (text or "").splitlines():
        line = line.strip()
        # "Device AA:BB:CC:DD:EE:FF Name with spaces"
        m = re.match(r"^Device\s+([0-9A-Fa-f:]{17})\s+(.*)$", line)
        if not m:
            continue
        out.append(BtDevice(mac=m.group(1).upper(), name=m.group(2).strip()))
    return out


def _device_info(mac: str) -> Optional[BtDevice]:
    """Fetch detailed info for one device via `bluetoothctl info <mac>`."""
    text = _bctl(f"info {mac}", timeout=5)
    if not text.strip() or "not available" in text.lower():
        return None
    name = ""
    paired = False
    trusted = False
    connected = False
    rssi: Optional[int] = None
    icon = ""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("Name:"):
            name = line.split(":", 1)[1].strip()
        elif line.startswith("Paired:"):
            paired = "yes" in line.lower()
        elif line.startswith("Trusted:"):
            trusted = "yes" in line.lower()
        elif line.startswith("Connected:"):
            connected = "yes" in line.lower()
        elif line.startswith("Icon:"):
            icon = line.split(":", 1)[1].strip()
        elif line.startswith("RSSI:"):
            try:
                rssi = int(line.split(":", 1)[1].strip())
            except ValueError:
                rssi = None
    return BtDevice(
        mac=mac.upper(),
        name=name,
        paired=paired,
        trusted=trusted,
        connected=connected,
        rssi=rssi,
        icon=icon,
    )


def _list_with_info(scope: str = "") -> list[BtDevice]:
    """`bluetoothctl devices [scope]` enriched with per-device info.

    Devices without a human-readable name are filtered out: those are almost
    always BLE anonymous advertisements with rotating random MACs (e.g. phones,
    earbuds, watches). They cannot be meaningfully paired from this list since
    the MAC may be different by the time the user taps Pair.  Paired devices
    are kept regardless so a previously-paired device with no cached name
    still appears.
    """
    arg = "devices" if not scope else f"devices {scope}"
    text = _bctl(arg, timeout=5)
    base = _parse_devices(text)
    out: list[BtDevice] = []
    for d in base:
        info = _device_info(d.mac) or d
        # bluez substitutes "AA-BB-CC-DD-EE-FF" (dashed MAC) for the name when
        # the device hasn't advertised one — treat that as nameless.
        mac_dashed = info.mac.replace(":", "-")
        nameless = (not info.name) or info.name == info.mac or info.name == mac_dashed
        if info.paired or not nameless:
            out.append(info)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints (read-only)
# ─────────────────────────────────────────────────────────────────────────────


@router.get("/status", response_model=BtAdapter)
async def status():
    """Adapter status (powered/discoverable/discovering)."""
    try:
        if is_sim_mode() or not _has_bluetoothctl():
            return BtAdapter(available=False, powered=False, discoverable=False, discovering=False)
        out = await asyncio.to_thread(_bctl, "show", 5)
        return _parse_show(out)
    except Exception:
        return BtAdapter(available=False, powered=False, discoverable=False, discovering=False)


@router.get("/devices", response_model=list[BtDevice])
async def devices():
    """All known devices (paired + currently visible)."""
    try:
        if is_sim_mode() or not _has_bluetoothctl():
            return []
        return await asyncio.to_thread(_list_with_info, "")
    except Exception:
        return []


@router.get("/devices/paired", response_model=list[BtDevice])
async def paired_devices():
    try:
        if is_sim_mode() or not _has_bluetoothctl():
            return []
        return await asyncio.to_thread(_list_with_info, "Paired")
    except Exception:
        return []


def _scan_blocking(seconds: int) -> list:
    seconds = max(1, min(int(seconds), 20))
    _bctl("power on", timeout=5)
    try:
        _run(
            ["bluetoothctl", "--timeout", str(seconds), "scan", "on"],
            timeout=seconds + 5,
        )
    except subprocess.TimeoutExpired:
        pass
    return _list_with_info("")


@router.get("/scan", response_model=list[BtDevice])
async def scan(seconds: int = 8):
    """Run a discovery for `seconds` (1..20) and return the device list."""
    try:
        if is_sim_mode() or not _has_bluetoothctl():
            return []
        return await asyncio.to_thread(_scan_blocking, seconds)
    except Exception:
        return []


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints (mutating)
# ─────────────────────────────────────────────────────────────────────────────


@router.post("/pair")
async def pair(req: BtMacRequest):
    if is_sim_mode():
        raise HTTPException(status_code=503, detail="Not supported in simulation mode")
    mac = _validate_mac(req.mac)
    if not _has_bluetoothctl():
        raise HTTPException(status_code=503, detail="bluetoothctl unavailable")

    # Pair after a fresh remove (Forget) only works if BlueZ has just
    # rediscovered the device. Run a short discovery first so `pair {mac}`
    # finds it.  scan must be running while pair is issued.
    def _do_pair() -> str:
        # Make sure adapter is on and start a 6s discovery in the foreground.
        _bctl("power on", timeout=5)
        try:
            _on_host(
                ["bluetoothctl", "--timeout", "6", "scan", "on"],
                timeout=10,
            )
        except subprocess.TimeoutExpired:
            pass
        return _bctl_script(
            [f"pair {mac}", f"trust {mac}", f"connect {mac}"],
            timeout=15,
        )

    try:
        out = await asyncio.to_thread(_do_pair)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Pair error: {e}")

    low = out.lower()
    success = "pairing successful" in low or "already paired" in low

    # Even if bluetoothctl reported failure, BlueZ + bt-agent + xpadneo can
    # finish pairing/connecting in the background a few seconds later.
    # Poll device state briefly before giving up.
    info = None
    if not success:
        for _ in range(8):  # ~8 * 0.5s = 4s
            await asyncio.sleep(0.5)
            try:
                info = await asyncio.to_thread(_device_info, mac)
            except Exception:
                info = None
            if info and (info.get("paired") or info.get("connected")):
                success = True
                break

    if success:
        _bump()
        if info is None:
            try:
                info = await asyncio.to_thread(_device_info, mac)
            except Exception:
                info = None
        return info or {"mac": mac}

    reason = ""
    for line in out.splitlines():
        ll = line.lower()
        if "failed" in ll or "error" in ll or "not available" in ll or "timeout" in ll:
            reason = line.strip()
            break
    raise HTTPException(
        status_code=400,
        detail=f"Pairing failed: {reason or 'check that the device is in pairing mode'}",
    )


@router.post("/connect")
async def connect(req: BtMacRequest):
    if is_sim_mode():
        raise HTTPException(status_code=503, detail="Not supported in simulation mode")
    mac = _validate_mac(req.mac)
    if not _has_bluetoothctl():
        raise HTTPException(status_code=503, detail="bluetoothctl unavailable")
    try:
        out = await asyncio.to_thread(
            _bctl_script,
            [
                "power on",
                f"connect {mac}",
            ],
            15,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Connect error: {e}")
    low = out.lower()
    if "connection successful" in low or "already connected" in low:
        _bump()
        try:
            info = await asyncio.to_thread(_device_info, mac)
            return info or {"mac": mac}
        except Exception:
            return {"mac": mac}
    reason = ""
    for line in out.splitlines():
        ll = line.lower()
        if "failed" in ll or "error" in ll or "not available" in ll:
            reason = line.strip()
            break
    raise HTTPException(status_code=400, detail=f"Connect failed: {reason or 'unknown'}")


@router.post("/disconnect")
async def disconnect(req: BtMacRequest):
    if is_sim_mode():
        raise HTTPException(status_code=503, detail="Not supported in simulation mode")
    mac = _validate_mac(req.mac)
    if not _has_bluetoothctl():
        raise HTTPException(status_code=503, detail="bluetoothctl unavailable")
    try:
        await asyncio.to_thread(_bctl, f"disconnect {mac}", 10)
        _bump()
        info = await asyncio.to_thread(_device_info, mac)
        return info or {"mac": mac}
    except Exception:
        return {"mac": mac}


@router.post("/remove")
async def remove(req: BtMacRequest):
    if is_sim_mode():
        raise HTTPException(status_code=503, detail="Not supported in simulation mode")
    mac = _validate_mac(req.mac)
    if not _has_bluetoothctl():
        raise HTTPException(status_code=503, detail="bluetoothctl unavailable")
    try:
        await asyncio.to_thread(_bctl, f"remove {mac}", 10)
        _bump()
    except Exception:
        pass
    return {"mac": mac, "removed": True}
