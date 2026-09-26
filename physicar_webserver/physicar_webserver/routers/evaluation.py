# SPDX-License-Identifier: LicenseRef-PhysiCar-Community-1.0
# Copyright (c) 2026 AICASTLE Inc.
# Licensed under the PhysiCar Community License 1.0 (see LICENSE).

"""Evaluation run executor — starts/stops the student process on behalf of the
simulator's evaluation session.

The simulator runs in its own container. Its evaluation session owns the
evaluation (scoring script, sim-time verdict, result) and starts the student's
code through this router (PHYSICAR_RUNNER_URL: POST /evaluation/run,
POST /evaluation/stop). Student code must still execute here, in the workspace:
this is where the ROS environment, ~/physicar_ws and the loopback web APIs live.
The simulator relays the NDJSON stream below into its own SSE (event: run), so
every viewer sees the output.

Reachability: uvicorn binds 127.0.0.1:8000; the workspace nginx proxies
/evaluation/ only from the simulator container (172.30.0.3 —
deploy/sim/etc/nginx/sites-available/physicar). Never from the browser.

Stream frames (one JSON object per line):
  {"phase": "log", "stream": "stdout"|"stderr", "line": "...", ["truncated": true]}
  {"phase": "exit", "exit_code": <int>}
"""

import asyncio
import json
import os
import signal
import time
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

router = APIRouter(prefix="/evaluation", tags=["Evaluation"])

_LINE_MAX = 300     # max length of a single log line
_RATE_MAX = 30      # max log lines per second per stream (excess dropped + 1 summary line)

_lock = asyncio.Lock()
_proc: Optional[asyncio.subprocess.Process] = None
_exit_code: Optional[int] = None


class RunRequest(BaseModel):
    command: str
    wall_limit_s: float = 390.0   # backstop only — the simulator's evaluation session owns the sim-time verdict


def _alive() -> bool:
    return _proc is not None and _proc.returncode is None


async def _kill(proc: asyncio.subprocess.Process) -> None:
    """SIGTERM the whole process group → 3 s grace → SIGKILL, then wait for the leader."""
    try:
        pgid = os.getpgid(proc.pid)
    except Exception:
        return
    for sig, grace in ((signal.SIGTERM, 3.0), (signal.SIGKILL, 1.0)):
        if proc.returncode is not None:
            return
        try:
            os.killpg(pgid, sig)
        except Exception:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=grace)
            return
        except asyncio.TimeoutError:
            continue


@router.post("/run")
async def run(req: RunRequest):
    """Start the student process and stream its output until it exits."""
    global _proc, _exit_code
    command = req.command.strip()
    if not command or len(command) > 200:
        raise HTTPException(status_code=400, detail="invalid command")
    wall_limit = max(10.0, min(float(req.wall_limit_s), 7230.0))
    async with _lock:
        if _alive():
            raise HTTPException(status_code=409, detail="already running")
        # bash -lc — a login shell, so ~ expansion and the ROS environment (bashrc)
        # are set up exactly like the student's terminal. Own session → the whole
        # tree can be killed as a group.
        proc = await asyncio.create_subprocess_exec(
            "bash", "-lc", command,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True, cwd=os.path.expanduser("~"),
        )
        _proc = proc
        _exit_code = None

    queue: asyncio.Queue = asyncio.Queue()

    async def pump(stream, name):
        win_start, win_count, dropped = time.monotonic(), 0, 0
        while True:
            raw = await stream.readline()
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
            now = time.monotonic()
            if now - win_start >= 1.0:
                if dropped:
                    await queue.put({"phase": "log", "stream": name,
                                     "line": f"... ({dropped} lines dropped)", "truncated": True})
                win_start, win_count, dropped = now, 0, 0
            if win_count >= _RATE_MAX:
                dropped += 1
                continue
            win_count += 1
            await queue.put({"phase": "log", "stream": name, "line": line[:_LINE_MAX]})

    async def backstop():
        try:
            await asyncio.wait_for(proc.wait(), timeout=wall_limit)
        except asyncio.TimeoutError:
            await _kill(proc)

    async def activity():
        # A running evaluation is student activity — the host's idle-stop heartbeat
        # only watches terminals and myapp.log, so mark it here every 30 s.
        marker = "/opt/physicar/userdata/.eval-active"
        while proc.returncode is None:
            try:
                with open(marker, "a"):
                    os.utime(marker, None)
            except OSError:
                pass
            await asyncio.sleep(30)

    async def gen():
        global _exit_code
        pumps = [asyncio.create_task(pump(proc.stdout, "stdout")),
                 asyncio.create_task(pump(proc.stderr, "stderr"))]
        guard = asyncio.create_task(backstop())
        beat = asyncio.create_task(activity())
        waiter = asyncio.create_task(proc.wait())
        try:
            while True:
                if waiter.done() and all(p.done() for p in pumps) and queue.empty():
                    break
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
                yield json.dumps(item, ensure_ascii=False) + "\n"
            rc = proc.returncode
            _exit_code = rc
            yield json.dumps({"phase": "exit", "exit_code": rc}) + "\n"
        finally:
            # The relay (sim_api) went away: keep the student process running — it is
            # not this side's decision to stop it; the backstop still bounds it.
            for p in pumps:
                p.cancel()
            beat.cancel()
            if not waiter.done():
                asyncio.create_task(_finish(proc, guard))
            else:
                guard.cancel()

    return StreamingResponse(gen(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


async def _finish(proc, guard):
    global _exit_code
    await proc.wait()
    _exit_code = proc.returncode
    guard.cancel()


@router.post("/stop")
async def stop():
    """Idempotent stop — succeeds quietly even if nothing is running. Returns once the
    process is gone, so an immediate re-run cannot race the running check."""
    proc = _proc
    if proc is not None and proc.returncode is None:
        await _kill(proc)
    return {"ok": True}


@router.get("/status")
async def status():
    alive = _alive()
    return {"running": alive, "exit_code": None if alive else _exit_code}
