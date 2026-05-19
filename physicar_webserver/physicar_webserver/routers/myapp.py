#!/usr/bin/env python3
#
# Copyright 2026 AICASTLE Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
MyApp Router — student-facing webapp (:5000) slot management.

``physicar-myapp.service`` runs ``/home/physicar/physicar_ws/userdata/myapp/run.sh``.
This router writes run.sh / log under
``/home/physicar/physicar_ws/userdata/myapp/`` and uses ``sudo systemctl`` to control the
systemd unit (start / stop / restart).
"""

import asyncio
import os
import os
import socket
from pathlib import Path

import subprocess
import json as _json
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

router = APIRouter(prefix="/settings/myapp", tags=["myapp"])


CONFIG_DIR = Path("/home/physicar/physicar_ws/userdata/myapp")
SCRIPT_FILE = CONFIG_DIR / "run.sh"
LOG_FILE = CONFIG_DIR / "log"

_MYAPP_SYSTEMD_UNIT = "physicar-myapp.service"
_MYAPP_SUPERVISOR_PROGRAM = "myapp"

MYAPP_PORT = 5000
PORT_PROBE_TIMEOUT = 0.2
LOG_TAIL_MAX = 5000
LOG_TAIL_DEFAULT = 200
SCRIPT_MAX_BYTES = 256 * 1024  # 256 KB


def _ensure_dir() -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass


def _read_script() -> str:
    try:
        return SCRIPT_FILE.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return ""


def _write_script(content: str) -> None:
    """Atomic write of run.sh + chmod 0755."""
    _ensure_dir()
    if not content.endswith("\n"):
        content = content + "\n"
    tmp = SCRIPT_FILE.with_suffix(".sh.tmp")
    tmp.write_text(content, encoding="utf-8")
    os.chmod(tmp, 0o755)
    tmp.replace(SCRIPT_FILE)


def _delete_script() -> None:
    try:
        SCRIPT_FILE.unlink()
    except FileNotFoundError:
        pass


def _is_running() -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(PORT_PROBE_TIMEOUT)
    try:
        return sock.connect_ex(("127.0.0.1", MYAPP_PORT)) == 0
    except OSError:
        return False
    finally:
        sock.close()


def _systemctl(action: str) -> None:
    if os.environ.get("CODESPACE_NAME"):
        cmd = ["supervisorctl", "-s", "unix:///tmp/supervisor.sock",
               action, _MYAPP_SUPERVISOR_PROGRAM]
    else:
        cmd = ["sudo", "systemctl", action, _MYAPP_SYSTEMD_UNIT]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            raise HTTPException(500, result.stderr.strip() or "service control failed")
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "service control timeout")


class MyAppConfig(BaseModel):
    script: str = Field(..., description="Bash script body (run.sh).")


class MyAppStatus(BaseModel):
    script: str
    exists: bool
    running: bool
    port: int


def _status() -> MyAppStatus:
    return MyAppStatus(
        script=_read_script(),
        exists=SCRIPT_FILE.is_file(),
        running=_is_running(),
        port=MYAPP_PORT,
    )


@router.get("", response_model=MyAppStatus)
async def get_myapp() -> MyAppStatus:
    return _status()


@router.put("", response_model=MyAppStatus)
async def set_myapp(cfg: MyAppConfig) -> MyAppStatus:
    """Replace run.sh and (re)start the host service."""
    body = cfg.script
    if len(body.encode("utf-8")) > SCRIPT_MAX_BYTES:
        raise HTTPException(400, f"script too large (>{SCRIPT_MAX_BYTES} bytes)")
    _write_script(body)
    _systemctl("restart")
    return _status()


@router.delete("", response_model=MyAppStatus)
async def clear_myapp() -> MyAppStatus:
    """Stop service and remove run.sh."""
    _systemctl("stop")
    _delete_script()
    return _status()


@router.post("/start", response_model=MyAppStatus)
async def start_myapp() -> MyAppStatus:
    if not SCRIPT_FILE.is_file():
        raise HTTPException(404, "run.sh does not exist")
    _systemctl("start")
    return _status()


@router.post("/stop", response_model=MyAppStatus)
async def stop_myapp() -> MyAppStatus:
    _systemctl("stop")
    return _status()


@router.post("/restart", response_model=MyAppStatus)
async def restart_myapp() -> MyAppStatus:
    if not SCRIPT_FILE.is_file():
        raise HTTPException(404, "run.sh does not exist")
    _systemctl("restart")
    return _status()


@router.get("/log")
async def get_myapp_log(
    request: Request,
    tail: int = Query(LOG_TAIL_DEFAULT, ge=1, le=LOG_TAIL_MAX),
    stream: bool = Query(False),
) -> dict:
    accept = request.headers.get("accept", "")
    if stream or "text/event-stream" in accept:
        return StreamingResponse(
            _log_stream(request, tail),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    if not LOG_FILE.exists():
        return {"lines": [], "size": 0}
    try:
        data = LOG_FILE.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        raise HTTPException(500, f"cannot read log: {e}") from e
    lines = data.splitlines()
    return {
        "lines": lines[-tail:],
        "size": len(data),
        "truncated": len(lines) > tail,
    }


async def _log_stream(request: Request, tail: int):
    """Server-Sent Events: emit existing tail then follow appends.

    Handles log truncation/rotation by detecting size shrink and
    reopening from offset 0.
    """
    poll = 0.5
    keepalive = 15.0
    last_event = asyncio.get_event_loop().time()

    f = None
    try:
        if LOG_FILE.exists():
            f = open(LOG_FILE, "r", encoding="utf-8", errors="replace")
            data = f.read()
            lines = data.splitlines()
            for line in lines[-tail:]:
                yield f"data: {line}\n\n"
                last_event = asyncio.get_event_loop().time()

        buf = ""
        while True:
            if await request.is_disconnected():
                return

            if f is None:
                if LOG_FILE.exists():
                    f = open(LOG_FILE, "r", encoding="utf-8", errors="replace")
                else:
                    await asyncio.sleep(poll)
                    continue

            try:
                st = os.fstat(f.fileno())
                pos = f.tell()
                if st.st_size < pos:
                    f.close()
                    f = open(LOG_FILE, "r", encoding="utf-8", errors="replace")
                    buf = ""
            except OSError:
                f.close()
                f = None
                await asyncio.sleep(poll)
                continue

            chunk = f.read()
            if chunk:
                buf += chunk
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    yield f"data: {line}\n\n"
                last_event = asyncio.get_event_loop().time()
            else:
                now = asyncio.get_event_loop().time()
                if now - last_event >= keepalive:
                    yield ": keepalive\n\n"
                    last_event = now
                await asyncio.sleep(poll)
    finally:
        if f is not None:
            f.close()


@router.delete("/log")
async def clear_myapp_log() -> dict:
    try:
        if LOG_FILE.exists():
            LOG_FILE.write_text("", encoding="utf-8")
    except OSError as e:
        raise HTTPException(500, f"cannot truncate log: {e}") from e
    return {"ok": True}
