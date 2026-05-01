#!/usr/bin/env python3
"""
Network router - WiFi scan/connect, network info, internet status, AP info.

Used by kiosk page, main app, and any other client that needs network control.
Endpoints are mounted at /network/* and kiosk.py exposes /kiosk/* aliases for
backward compatibility.
"""

import subprocess
import socket
import re
import time
from typing import Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from physicar_webserver.auth import get_password
from physicar_webserver.ros_bridge import get_ros_bridge

router = APIRouter(prefix="/network", tags=["Network"])


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _is_sim() -> bool:
    """Check if running in SIM mode (set by sim.launch.py)."""
    try:
        bridge = get_ros_bridge()
        node = bridge._node
        return bool(node.has_parameter("use_sim_time") and
                    node.get_parameter("use_sim_time").value)
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Models
# ─────────────────────────────────────────────────────────────────────────────

class WifiNetwork(BaseModel):
    ssid: str
    signal: int  # dBm
    security: str  # "open" | "wpa" | "wep" | "enterprise"
    frequency: int  # MHz


class WifiConnectRequest(BaseModel):
    ssid: str
    password: Optional[str] = None
    identity: Optional[str] = None  # 802.1X username


class NetworkInfo(BaseModel):
    wifi_ip: str
    wifi_mac: str
    wifi_ssid: str
    eth_ip: str
    eth_mac: str
    hostname: str
    mdns: str


# ─────────────────────────────────────────────────────────────────────────────
# WiFi scan helpers
# ─────────────────────────────────────────────────────────────────────────────

def _decode_iw_ssid(raw: str) -> str:
    try:
        decoded = re.sub(
            r'\\x([0-9a-fA-F]{2})',
            lambda m: chr(int(m.group(1), 16)),
            raw
        )
        return decoded.encode('latin-1').decode('utf-8')
    except (UnicodeDecodeError, UnicodeEncodeError):
        return raw


def _parse_nmcli_scan() -> list[WifiNetwork]:
    # Best-effort rescan. If the radio is busy or we just rescanned, nmcli
    # may exit non-zero or hit timeout — that's fine, we'll fall through to
    # the cached list query below. Treating rescan failure as fatal is the
    # main reason the UI used to show "Scan failed" 1-in-5 times.
    try:
        subprocess.run(
            ["nmcli", "dev", "wifi", "rescan", "ifname", "wlan0"],
            capture_output=True, text=True, timeout=10
        )
    except Exception:
        pass

    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY,FREQ", "dev", "wifi", "list", "ifname", "wlan0"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            return []
    except Exception:
        return []

    if not result.stdout.strip():
        # Cached list empty — give the radio a moment and re-query once.
        time.sleep(1.5)
        try:
            result = subprocess.run(
                ["nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY,FREQ", "dev", "wifi", "list", "ifname", "wlan0"],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode != 0 or not result.stdout.strip():
                return []
        except Exception:
            return []

    seen = set()
    networks: list[WifiNetwork] = []
    for line in result.stdout.strip().split('\n'):
        if not line.strip():
            continue
        parts = line.split(':')
        if len(parts) < 4:
            continue
        ssid = parts[0].strip()
        if not ssid or ssid == '--' or ssid in seen:
            continue
        seen.add(ssid)
        try:
            sig_pct = int(parts[1])
        except ValueError:
            sig_pct = 0
        signal_dbm = -100 + sig_pct
        sec_str = parts[2].strip().upper()
        if '802.1X' in sec_str:
            security = 'enterprise'
        elif 'WPA' in sec_str or 'SAE' in sec_str:
            security = 'wpa'
        elif 'WEP' in sec_str:
            security = 'wep'
        else:
            security = 'open'
        try:
            frequency = int(parts[3].strip().split()[0])
        except (ValueError, IndexError):
            frequency = 0
        networks.append(WifiNetwork(
            ssid=ssid, signal=signal_dbm, security=security, frequency=frequency
        ))
    networks.sort(key=lambda x: x.signal, reverse=True)
    return networks


def _parse_iw_scan_fallback() -> list[WifiNetwork]:
    try:
        result = subprocess.run(
            ["iw", "dev", "wlan0", "scan"],
            capture_output=True, text=True, timeout=15
        )
        output = result.stdout
        if 'busy' in output.lower() or not output.strip():
            return []
    except Exception:
        return []

    networks = []
    current = {}
    for line in output.split('\n'):
        line = line.strip()
        if line.startswith('BSS '):
            if current.get('ssid'):
                networks.append(current)
            mac_match = re.match(r'BSS ([0-9a-f:]+)', line)
            current = {
                'mac': mac_match.group(1) if mac_match else '',
                'ssid': '',
                'signal': -100,
                'security': 'open',
                'frequency': 0,
            }
        elif 'SSID:' in line:
            ssid = line.split('SSID:', 1)[1].strip()
            current['ssid'] = _decode_iw_ssid(ssid)
        elif 'signal:' in line:
            sig_match = re.search(r'(-?\d+)', line)
            if sig_match:
                current['signal'] = int(sig_match.group(1))
        elif 'freq:' in line:
            freq_match = re.search(r'(\d+)', line)
            if freq_match:
                current['frequency'] = int(float(freq_match.group(1)))
        elif 'Authentication suites:' in line:
            if '802.1X' in line:
                current['security'] = 'enterprise'
            elif current['security'] != 'enterprise':
                current['security'] = 'wpa'
        elif 'WPA' in line or 'RSN' in line:
            if current['security'] != 'enterprise':
                current['security'] = 'wpa'
        elif 'WEP' in line:
            if current['security'] == 'open':
                current['security'] = 'wep'
    if current.get('ssid'):
        networks.append(current)

    seen = set()
    out: list[WifiNetwork] = []
    for n in networks:
        if n['ssid'] and n['ssid'] not in seen:
            seen.add(n['ssid'])
            out.append(WifiNetwork(
                ssid=n['ssid'],
                signal=n['signal'],
                security=n['security'],
                frequency=n['frequency'],
            ))
    out.sort(key=lambda x: x.signal, reverse=True)
    return out


def _parse_iw_scan() -> list[WifiNetwork]:
    networks = _parse_nmcli_scan()
    if networks:
        return networks
    return _parse_iw_scan_fallback()


# ─────────────────────────────────────────────────────────────────────────────
# Network info helpers
# ─────────────────────────────────────────────────────────────────────────────

def _get_network_info() -> NetworkInfo:
    def get_interface_info(interface: str) -> tuple[str, str]:
        ip_addr = ""
        mac_addr = ""
        try:
            result = subprocess.run(
                ["ip", "addr", "show", interface],
                capture_output=True, text=True, timeout=5
            )
            ip_match = re.search(r'inet (\d+\.\d+\.\d+\.\d+)', result.stdout)
            if ip_match:
                ip_addr = ip_match.group(1)
            mac_match = re.search(r'link/ether ([0-9a-f:]+)', result.stdout)
            if mac_match:
                mac_addr = mac_match.group(1)
        except Exception:
            pass
        return ip_addr, mac_addr

    wifi_ip, wifi_mac = get_interface_info("wlan0")
    eth_ip, eth_mac = get_interface_info("eth0")

    wifi_ssid = ""
    try:
        result = subprocess.run(
            ["iw", "dev", "wlan0", "link"],
            capture_output=True, text=True, timeout=5
        )
        ssid_match = re.search(r'SSID: (.+)', result.stdout)
        if ssid_match:
            wifi_ssid = ssid_match.group(1).strip()
    except Exception:
        pass

    try:
        hostname = socket.gethostname()
    except Exception:
        hostname = "physicar"

    return NetworkInfo(
        wifi_ip=wifi_ip,
        wifi_mac=wifi_mac,
        wifi_ssid=wifi_ssid,
        eth_ip=eth_ip,
        eth_mac=eth_mac,
        hostname=hostname,
        mdns=f"{hostname}.local",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/info", response_model=NetworkInfo)
async def network_info():
    """Get current network information (IP, hostname, mDNS, connected SSID)."""
    if _is_sim():
        try:
            hostname = socket.gethostname()
        except Exception:
            hostname = "physicar"
        eth_ip, eth_mac = "", ""
        try:
            result = subprocess.run(
                ["ip", "addr", "show", "eth0"],
                capture_output=True, text=True, timeout=5
            )
            ip_match = re.search(r'inet (\d+\.\d+\.\d+\.\d+)', result.stdout)
            if ip_match:
                eth_ip = ip_match.group(1)
            mac_match = re.search(r'link/ether ([0-9a-f:]+)', result.stdout)
            if mac_match:
                eth_mac = mac_match.group(1)
        except Exception:
            pass
        return NetworkInfo(
            wifi_ip="", wifi_mac="", wifi_ssid="",
            eth_ip=eth_ip, eth_mac=eth_mac,
            hostname=hostname, mdns=f"{hostname}.local",
        )
    return _get_network_info()


@router.get("/wifi/scan", response_model=list[WifiNetwork])
async def wifi_scan():
    """Scan for available WiFi networks."""
    if _is_sim():
        return []
    return _parse_iw_scan()


@router.get("/internet")
async def internet_status():
    """Check internet connectivity via WiFi (wlan0) and Ethernet (eth0)."""
    if _is_sim():
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(2)
            s.connect(("8.8.8.8", 53))
            s.close()
            internet = True
        except Exception:
            internet = False
        return {"wifi": False, "ethernet": internet}

    def _check_interface(interface: str) -> bool:
        try:
            result = subprocess.run(
                ["ip", "addr", "show", interface],
                capture_output=True, text=True, timeout=3
            )
            if 'inet ' not in result.stdout:
                return False
        except Exception:
            return False
        try:
            ip_match = re.search(r'inet (\d+\.\d+\.\d+\.\d+)', result.stdout)
            if not ip_match:
                return False
            local_ip = ip_match.group(1)
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(2)
            s.bind((local_ip, 0))
            s.connect(("8.8.8.8", 53))
            s.close()
            return True
        except Exception:
            return False

    return {
        "wifi": _check_interface("wlan0"),
        "ethernet": _check_interface("eth0"),
    }


# ─────────────────────────────────────────────────────────────────────────────
# TLS cert status
# ─────────────────────────────────────────────────────────────────────────────
#
# The host-side cert watcher (physicar.sh, fetch_cert_once loop) keeps
# /etc/nginx/ssl/le.{crt,key} in sync with https://device-cert.physicar.ai.
# When the on-disk cert is a valid LE cert, /run/physicar/le-cert-valid is
# touched.  When it's missing/expired/self-signed, that flag is removed and
# le.{crt,key} are reverted to the self-signed seed.
#
# Inside the container we cannot see /etc/nginx/ssl/ directly (not mounted),
# but /var/run is bind-mounted from the host's /var/run (== /run on systemd
# distros), so the watcher's flag and PID files are reachable via /var/run.
# For cert metadata (expires_at, issuer) we shell into the host mount
# namespace via nsenter — same trick the wifi_connect handler uses.

_LE_VALID_FLAG = "/var/run/physicar/le-cert-valid"
_CERT_REFRESH_PID_FILE = "/var/run/physicar/cert-fetcher.pid"
_HOST_LE_CRT_PATH = "/etc/nginx/ssl/le.crt"  # path on host


def _read_cert_metadata() -> dict:
    """Run openssl in the host mount namespace to read le.crt fields."""
    import datetime as _dt

    out: dict = {"present": False}
    try:
        r = subprocess.run(
            ["nsenter", "-t", "1", "-m", "--",
             "openssl", "x509", "-in", _HOST_LE_CRT_PATH, "-noout",
             "-enddate", "-issuer", "-subject"],
            capture_output=True, text=True, timeout=3,
        )
        if r.returncode != 0:
            return out
        out["present"] = True
        for line in (r.stdout or "").splitlines():
            if line.startswith("notAfter="):
                ts = line[len("notAfter="):].strip()
                try:
                    dt = _dt.datetime.strptime(ts, "%b %d %H:%M:%S %Y %Z")
                    out["expires_at"] = int(
                        dt.replace(tzinfo=_dt.timezone.utc).timestamp()
                    )
                except Exception:
                    pass
            elif line.startswith("issuer="):
                out["issuer"] = line[len("issuer="):].strip()
            elif line.startswith("subject="):
                out["subject"] = line[len("subject="):].strip()
        if "issuer" in out and "subject" in out:
            out["is_self_signed"] = (out["issuer"] == out["subject"])
    except Exception:
        pass
    return out


@router.get("/cert-status")
async def cert_status():
    """
    Report on-disk TLS cert state for the kiosk gate overlay.

    Returns:
        valid: True iff the host watcher last saw a non-expired LE cert
               covering device.physicar.ai (i.e. /run/physicar/le-cert-valid
               touchfile present).
        is_self_signed: True iff the on-disk cert is the self-signed seed.
        expires_at: unix epoch (seconds, UTC) of cert notAfter, or null.
        days_left: floor((expires_at - now) / 86400). Negative if expired.
        issuer: openssl -issuer string (best-effort).
    """
    import os
    import time as _time

    if _is_sim():
        # SIM mode runs without nginx/cert; treat as "valid" so kiosk
        # never shows the gate during simulation.
        return {
            "valid": True,
            "is_self_signed": False,
            "expires_at": None,
            "days_left": None,
            "issuer": "sim",
        }

    valid = os.path.exists(_LE_VALID_FLAG)
    meta = _read_cert_metadata()
    expires_at = meta.get("expires_at")
    days_left = None
    if isinstance(expires_at, int):
        days_left = (expires_at - int(_time.time())) // 86400

    return {
        "valid": valid,
        "is_self_signed": bool(meta.get("is_self_signed", False)),
        "expires_at": expires_at,
        "days_left": days_left,
        "issuer": meta.get("issuer"),
    }


@router.post("/cert-refresh")
async def cert_refresh():
    """
    Ask the host cert-watcher (physicar.sh) to fetch a new cert immediately
    instead of waiting up to 3 minutes for its next tick.

    Implementation: send SIGUSR1 to the PID stored in
    /run/physicar/cert-fetcher.pid.  The watcher's USR1 trap kills its inner
    `sleep`, which makes the loop fall through to fetch_cert_once().

    Always returns 200 (best-effort); the kiosk will discover whether it
    worked by polling /network/cert-status.
    """
    import os
    import signal

    if _is_sim():
        return {"signalled": False, "reason": "sim"}

    try:
        with open(_CERT_REFRESH_PID_FILE, "r") as f:
            pid = int(f.read().strip())
        os.kill(pid, signal.SIGUSR1)
        return {"signalled": True, "pid": pid}
    except FileNotFoundError:
        return {"signalled": False, "reason": "watcher not running"}
    except (ValueError, ProcessLookupError, PermissionError) as e:
        return {"signalled": False, "reason": str(e)}


@router.get("/ap")
async def ap_info():
    """Get AP (hotspot) information: SSID, password, URL, IP."""
    if _is_sim():
        return {"ssid": "", "password": "", "ip": "", "url": "", "active": False}

    try:
        ap_ssid = socket.gethostname()
    except Exception:
        ap_ssid = "physicar"

    ap_password = get_password()
    ap_ip = "10.42.0.1"
    active = False
    try:
        result = subprocess.run(
            ["ip", "addr", "show", "ap0"],
            capture_output=True, text=True, timeout=5
        )
        ip_match = re.search(r'inet (\d+\.\d+\.\d+\.\d+)', result.stdout)
        if ip_match:
            ap_ip = ip_match.group(1)
            active = True
    except Exception:
        pass

    return {
        "ssid": ap_ssid,
        "password": ap_password,
        "ip": ap_ip,
        "url": "physicar.local",
        "active": active,
    }


@router.post("/wifi/connect")
async def wifi_connect(request: WifiConnectRequest):
    """
    Connect to a WiFi network using nmcli (preferred) or netplan fallback.
    """
    if _is_sim():
        raise HTTPException(status_code=501, detail="WiFi control not available in simulation mode")

    def run_on_host(cmd: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
        full_cmd = ["nsenter", "-t", "1", "-m", "-u", "-n", "--"] + cmd
        return subprocess.run(full_cmd, capture_output=True, text=True, timeout=timeout)

    is_enterprise = bool(request.identity)

    # 1. Try nmcli
    try:
        if is_enterprise:
            con_name = f"wifi-{request.ssid[:20]}"
            run_on_host(["nmcli", "connection", "delete", con_name], timeout=10)
            cmd = [
                "nmcli", "connection", "add",
                "type", "wifi",
                "con-name", con_name,
                "ifname", "wlan0",
                "ssid", request.ssid,
                "--",
                "wifi-sec.key-mgmt", "wpa-eap",
                "802-1x.eap", "peap",
                "802-1x.phase2-auth", "mschapv2",
                "802-1x.identity", request.identity,
                "802-1x.password", request.password or "",
            ]
            result = run_on_host(cmd, timeout=30)
            if result.returncode == 0:
                result = run_on_host(["nmcli", "connection", "up", con_name], timeout=30)
                if result.returncode == 0:
                    _bump_network()
                    return {"success": True, "message": f"Connected to {request.ssid}"}
                error_msg = result.stderr.strip()
                run_on_host(["nmcli", "connection", "delete", con_name], timeout=10)
                if any(x in error_msg.lower() for x in ["password", "auth", "secrets"]):
                    raise HTTPException(status_code=401, detail="Wrong username or password")
                raise HTTPException(status_code=400, detail=error_msg or "Connection failed")
            else:
                error_msg = result.stderr.strip() or result.stdout.strip()
                if not any(x in error_msg for x in [
                    "No such file or directory",
                    "NetworkManager is not running",
                    "not available",
                    "unmanaged",
                ]):
                    raise HTTPException(status_code=400, detail=error_msg)
        else:
            cmd = ["nmcli", "dev", "wifi", "connect", request.ssid, "ifname", "wlan0"]
            if request.password:
                cmd.extend(["password", request.password])
            result = run_on_host(cmd, timeout=30)
            if result.returncode == 0:
                _bump_network()
                return {"success": True, "message": f"Connected to {request.ssid}"}
            error_msg = result.stderr.strip()
            if any(x in error_msg for x in [
                "No such file or directory",
                "NetworkManager is not running",
                "No network with SSID",
                "not available",
                "unmanaged",
            ]):
                pass  # fall through to netplan
            elif "Secrets were required" in error_msg or "password" in error_msg.lower():
                raise HTTPException(status_code=401, detail="Wrong password")
            elif error_msg:
                raise HTTPException(status_code=400, detail=error_msg)
    except HTTPException:
        raise
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=408, detail="Connection timeout")
    except FileNotFoundError:
        pass

    # 2. Netplan fallback
    try:
        psk = request.password or ""
        if is_enterprise:
            netplan_config = f'''network:
  version: 2
  wifis:
    wlan0:
      optional: true
      dhcp4: true
      regulatory-domain: "KR"
      access-points:
        "{request.ssid}":
          auth:
            key-management: "eap"
            method: "peap"
            identity: "{request.identity}"
            password: "{psk}"
'''
        else:
            netplan_config = f'''network:
  version: 2
  wifis:
    wlan0:
      optional: true
      dhcp4: true
      regulatory-domain: "KR"
      access-points:
        "{request.ssid}":
          auth:
            key-management: "psk"
            password: "{psk}"
'''
        config_path = "/etc/netplan/60-wifi-kiosk.yaml"
        write_cmd = ["bash", "-c", f"cat > {config_path} << 'NETPLAN_EOF'\n{netplan_config}NETPLAN_EOF"]
        result = run_on_host(write_cmd)
        if result.returncode != 0:
            raise HTTPException(status_code=500, detail=f"Failed to write config: {result.stderr}")
        run_on_host(["chmod", "600", config_path])
        result = run_on_host(["netplan", "apply"], timeout=30)
        if result.returncode != 0:
            run_on_host(["rm", "-f", config_path])
            error_msg = result.stderr.strip()
            if "password" in error_msg.lower() or "invalid" in error_msg.lower():
                raise HTTPException(status_code=401, detail="Invalid password format")
            raise HTTPException(status_code=400, detail=f"Netplan apply failed: {error_msg}")
        time.sleep(5)
        check_result = run_on_host(["iw", "dev", "wlan0", "link"])
        if request.ssid in check_result.stdout:
            _bump_network()
            return {"success": True, "message": f"Connected to {request.ssid}"}
        elif "Not connected" in check_result.stdout:
            raise HTTPException(status_code=401, detail="Connection failed - wrong password")
        else:
            _bump_network()
            return {"success": True, "message": f"Connecting to {request.ssid}..."}
    except HTTPException:
        raise
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=408, detail="Connection timeout")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Connection error: {str(e)}")


@router.get("/password")
async def device_password():
    """Get the device password (for AP and login)."""
    return {"password": get_password()}


# ─────────────────────────────────────────────────────────────────────────────
# Saved WiFi connections (NetworkManager)
# ─────────────────────────────────────────────────────────────────────────────

class SavedConnection(BaseModel):
    name: str       # NetworkManager connection name
    ssid: str       # SSID (may differ from name)
    autoconnect: bool
    active: bool    # currently active


def _run_on_host(cmd: list[str], timeout: int = 10) -> subprocess.CompletedProcess:
    full_cmd = ["nsenter", "-t", "1", "-m", "-u", "-n", "--"] + cmd
    return subprocess.run(full_cmd, capture_output=True, text=True, timeout=timeout)


@router.get("/wifi/saved", response_model=list[SavedConnection])
async def wifi_saved_list():
    """List saved WiFi connections (NetworkManager)."""
    if _is_sim():
        return []
    try:
        # Get list of all wifi connections
        result = _run_on_host(
            ["nmcli", "-t", "-f", "NAME,TYPE,AUTOCONNECT,ACTIVE", "connection", "show"],
            timeout=10,
        )
        if result.returncode != 0:
            return []
    except Exception:
        return []

    saved: list[SavedConnection] = []
    for line in result.stdout.strip().split("\n"):
        if not line.strip():
            continue
        # nmcli -t escapes ':' inside fields with backslash
        parts = re.split(r'(?<!\\):', line)
        parts = [p.replace('\\:', ':') for p in parts]
        if len(parts) < 4:
            continue
        name, ctype, autoconn, active = parts[0], parts[1], parts[2], parts[3]
        if ctype != "802-11-wireless":
            continue
        # Resolve SSID via connection details (may contain non-ASCII)
        ssid = name
        try:
            det = _run_on_host(
                ["nmcli", "-t", "-g", "802-11-wireless.ssid", "connection", "show", name],
                timeout=5,
            )
            if det.returncode == 0 and det.stdout.strip():
                ssid = det.stdout.strip()
        except Exception:
            pass
        saved.append(SavedConnection(
            name=name,
            ssid=ssid,
            autoconnect=(autoconn.lower() == "yes"),
            active=(active.lower() == "yes"),
        ))
    return saved


@router.delete("/wifi/saved/{name}")
async def wifi_saved_delete(name: str):
    """Delete a saved WiFi connection by NetworkManager connection name."""
    if _is_sim():
        raise HTTPException(status_code=501, detail="WiFi control not available in simulation mode")
    try:
        result = _run_on_host(["nmcli", "connection", "delete", name], timeout=10)
        if result.returncode != 0:
            err = (result.stderr or result.stdout).strip()
            if "unknown connection" in err.lower() or "not found" in err.lower():
                raise HTTPException(status_code=404, detail=f"Connection '{name}' not found")
            raise HTTPException(status_code=400, detail=err or "Delete failed")
        _bump_network()
        return {"success": True, "message": f"Removed {name}"}
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=408, detail="Delete timeout")
    except FileNotFoundError:
        raise HTTPException(status_code=501, detail="nmcli not available")


class WifiActivateRequest(BaseModel):
    name: str  # NetworkManager connection name


@router.post("/wifi/activate")
async def wifi_activate(request: WifiActivateRequest):
    """Activate (bring up) an existing saved WiFi connection.

    Used when the UI clicks a *saved* network: NetworkManager already has
    the credentials (PSK/EAP/identity) stored, so we just `nmcli connection
    up` it instead of re-prompting the user for a password.
    """
    if _is_sim():
        raise HTTPException(status_code=501, detail="WiFi control not available in simulation mode")
    try:
        result = _run_on_host(
            ["nmcli", "connection", "up", request.name, "ifname", "wlan0"],
            timeout=30,
        )
        if result.returncode != 0:
            err = (result.stderr or result.stdout).strip()
            low = err.lower()
            if "unknown connection" in low or "not found" in low:
                raise HTTPException(status_code=404, detail=f"Connection '{request.name}' not found")
            if "secrets" in low or "password" in low or "auth" in low:
                # Stored secret missing/invalid — ask UI to fall back to password prompt.
                raise HTTPException(status_code=401, detail="Saved credentials invalid; password required")
            raise HTTPException(status_code=400, detail=err or "Activate failed")
        _bump_network()
        return {"success": True, "message": f"Activated {request.name}"}
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=408, detail="Activate timeout")
    except FileNotFoundError:
        raise HTTPException(status_code=501, detail="nmcli not available")


# ─────────────────────────────────────────────────────────────────────────────
# SSE broadcaster — pushes combined network snapshot to all clients.
# Each tab/page (/, /kiosk, laptop) opens one EventSource on /network/stream
# and stays in sync without polling. Mutating endpoints call _bump() to force
# an immediate re-poll instead of waiting for the next interval.
# ─────────────────────────────────────────────────────────────────────────────

import asyncio as _asyncio
import json as _json
from typing import Optional as _Optional
from fastapi import Request as _Request
from fastapi.responses import StreamingResponse as _StreamingResponse


async def _build_snapshot() -> dict:
    """Aggregate everything the network panel renders into one payload.

    The endpoint handlers below do blocking subprocess calls (nsenter, nmcli,
    iw, socket.connect). Running them directly inside the event loop starves
    every other request — any deepracer/calibration call concurrent with the
    5 s poll cycle hits its 2 s service timeout. So we drive each handler
    through asyncio.to_thread (and bury sync work in a worker thread).
    """
    def _dump(obj):
        if obj is None:
            return None
        if hasattr(obj, "model_dump"):
            return obj.model_dump()
        if hasattr(obj, "dict"):
            return obj.dict()
        return obj

    # The endpoint coroutines themselves are async but contain blocking IO.
    # Wrap each call in to_thread by running the coroutine in a fresh loop
    # within a worker. Simpler: extract the non-blocking shape using the
    # sync helpers where they exist, otherwise run the coroutine via
    # asyncio.run in a thread (cheap; nothing here is recursive).
    def _run_coro(coro_factory):
        return _asyncio.run(coro_factory())

    snap: dict = {}
    try:
        snap["info"] = _dump(await _asyncio.to_thread(_run_coro, network_info))
    except Exception as e:
        snap["info"] = {"error": str(e)}
    try:
        snap["internet"] = await _asyncio.to_thread(_run_coro, internet_status)
    except Exception as e:
        snap["internet"] = {"error": str(e)}
    try:
        snap["ap"] = await _asyncio.to_thread(_run_coro, ap_info)
    except Exception as e:
        snap["ap"] = {"error": str(e)}
    try:
        saved = await _asyncio.to_thread(_run_coro, wifi_saved_list)
        snap["saved"] = [_dump(s) for s in saved]
    except Exception as e:
        snap["saved"] = []

    # Bluetooth: adapter status + paired list (fast).  Discovery is excluded
    # because it's an 8-second blocking scan triggered only by user action.
    try:
        from physicar_webserver.routers import bluetooth as _bt
        snap["bt_status"] = _dump(await _asyncio.to_thread(_run_coro, _bt.status))
        bt_devs = await _asyncio.to_thread(_run_coro, _bt.devices)
        snap["bt_devices"] = [_dump(d) for d in bt_devs]
    except Exception as e:
        snap["bt_status"] = None
        snap["bt_devices"] = []
    return snap


class _NetworkBroadcaster:
    """Single shared poller for the combined network snapshot.

    Many SSE clients can subscribe; only one collection cycle per interval.
    `bump()` forces an immediate re-poll (used after WiFi connect/forget so
    other tabs see the change in <100 ms).
    """
    POLL_INTERVAL = 5.0  # seconds; bump() handles instantaneous updates

    def __init__(self):
        self._last_payload: _Optional[str] = None
        self._subscribers: set = set()
        self._task: _Optional[_asyncio.Task] = None
        self._wake = _asyncio.Event()

    def subscribe(self) -> "_asyncio.Queue":
        q: _asyncio.Queue = _asyncio.Queue(maxsize=4)
        self._subscribers.add(q)
        if self._last_payload is not None:
            try:
                q.put_nowait(self._last_payload)
            except _asyncio.QueueFull:
                pass
        self._ensure_running()
        return q

    def unsubscribe(self, q):
        self._subscribers.discard(q)

    def bump(self):
        self._wake.set()

    def _ensure_running(self):
        if self._task is None or self._task.done():
            self._task = _asyncio.create_task(self._run())

    def _emit(self, payload: str):
        for q in list(self._subscribers):
            try:
                q.put_nowait(payload)
            except _asyncio.QueueFull:
                pass

    async def _run(self):
        try:
            while self._subscribers:
                try:
                    snap = await _build_snapshot()
                    payload = _json.dumps(snap, default=str, sort_keys=True)
                    if payload != self._last_payload:
                        self._last_payload = payload
                        self._emit(payload)
                except Exception as e:
                    self._emit(_json.dumps({"error": str(e)}))
                try:
                    await _asyncio.wait_for(self._wake.wait(), timeout=self.POLL_INTERVAL)
                except _asyncio.TimeoutError:
                    pass
                self._wake.clear()
        except _asyncio.CancelledError:
            pass


_net_broadcaster: _Optional[_NetworkBroadcaster] = None


def _get_net_broadcaster() -> _NetworkBroadcaster:
    global _net_broadcaster
    if _net_broadcaster is None:
        _net_broadcaster = _NetworkBroadcaster()
    return _net_broadcaster


def _bump_network():
    try:
        _get_net_broadcaster().bump()
    except Exception:
        pass


@router.get("/stream")
async def network_stream(request: _Request):
    """SSE stream of the combined network snapshot.

    Emits whenever the snapshot changes (poll every 5s + bump on POST/DELETE).
    Each event payload is JSON: {info, internet, ap, saved}.
    """
    bcaster = _get_net_broadcaster()

    async def gen():
        q = bcaster.subscribe()
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await _asyncio.wait_for(q.get(), timeout=15.0)
                    yield f"data: {payload}\n\n"
                except _asyncio.TimeoutError:
                    # heartbeat to keep proxies from closing the connection
                    yield ": ping\n\n"
        except _asyncio.CancelledError:
            pass
        finally:
            bcaster.unsubscribe(q)

    return _StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
