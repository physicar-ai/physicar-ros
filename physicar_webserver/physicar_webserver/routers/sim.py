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
Sim Router — GitHub Codespaces machine management + open-external.

Endpoints live at ``/api/host/`` to match existing frontend calls.
Only functional when ``CODESPACE_NAME`` env var is set (i.e. running in
GitHub Codespaces).  On device they return 400.
"""

import json
import os
import subprocess
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

router = APIRouter(prefix="/api/host", tags=["sim"])

CODESPACE_NAME = os.environ.get("CODESPACE_NAME", "")


@router.get("/machines")
async def get_machines():
    """List available machine types + current machine info."""
    if not CODESPACE_NAME:
        raise HTTPException(400, "Not running in Codespace")

    try:
        cs_result = subprocess.run(
            ["gh", "api", f"user/codespaces/{CODESPACE_NAME}",
             "--jq", "{machine: .machine}"],
            capture_output=True, text=True, timeout=30,
        )
        if cs_result.returncode != 0:
            raise HTTPException(502, cs_result.stderr.strip())

        cs = json.loads(cs_result.stdout)
        current_machine = cs.get("machine", {})

        machines_result = subprocess.run(
            ["gh", "api", f"user/codespaces/{CODESPACE_NAME}/machines",
             "--jq", ".machines[] | {name, display_name, cpus, memory_gb: (.memory_in_bytes/1073741824), storage_gb: (.storage_in_bytes/1073741824)}"],
            capture_output=True, text=True, timeout=30,
        )
        if machines_result.returncode != 0:
            raise HTTPException(502, machines_result.stderr.strip())

        machines = []
        for line in machines_result.stdout.strip().split("\n"):
            if line:
                machines.append(json.loads(line))

        billable_owner = None
        repo = os.environ.get("GITHUB_REPOSITORY", "")
        if repo:
            bo_result = subprocess.run(
                ["gh", "api", f"/repos/{repo}/codespaces",
                 "--jq", f'.codespaces[] | select(.name == "{CODESPACE_NAME}") | .billable_owner.login'],
                capture_output=True, text=True, timeout=30,
            )
            if bo_result.returncode == 0 and bo_result.stdout.strip():
                billable_owner = bo_result.stdout.strip()

        return {
            "machines": machines,
            "current": current_machine.get("name", ""),
            "cpus": current_machine.get("cpus", 0),
            "memory_gb": int(current_machine.get("memory_in_bytes", 0) / 1073741824),
            "storage_gb": int(current_machine.get("storage_in_bytes", 0) / 1073741824),
            "billable_owner": billable_owner,
        }
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "GitHub API timeout")


class MachineChangeRequest(BaseModel):
    machine: str


@router.post("/machine")
async def change_machine(body: MachineChangeRequest):
    """Change Codespace machine type and restart."""
    if not CODESPACE_NAME:
        raise HTTPException(400, "Not running in Codespace")

    machine_name = body.machine.strip()
    if not machine_name:
        raise HTTPException(400, "machine required")

    try:
        edit = subprocess.run(
            ["gh", "codespace", "edit", "-c", CODESPACE_NAME, "-m", machine_name],
            capture_output=True, text=True, timeout=60,
        )
        if edit.returncode != 0:
            raise HTTPException(500, edit.stderr.strip())

        subprocess.run(
            ["gh", "api", "--method", "POST",
             f"user/codespaces/{CODESPACE_NAME}/start"],
            capture_output=True, text=True, timeout=60,
        )

        return {"success": True, "machine": machine_name}
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "GitHub API timeout")


@router.get("/open-external")
async def open_external(url: str = Query("", description="URL to open")):
    """Open a URL in the host browser using VS Code's $BROWSER."""
    url = url.strip()
    if not url:
        raise HTTPException(400, "url required")

    # Read VS Code env vars saved by postAttach.sh
    env_vars = {}
    try:
        for line in Path("/tmp/vscode-env").read_text().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                env_vars[k] = v
    except OSError:
        pass

    browser = env_vars.get("BROWSER", os.environ.get("BROWSER", "xdg-open"))

    # Follow redirects to get final URL
    final_url = url
    try:
        async with httpx.AsyncClient() as client:
            r = await client.head(url, follow_redirects=False, timeout=5)
            if r.status_code in (301, 302, 303, 307, 308):
                final_url = str(r.headers.get("location", url))
    except Exception:
        pass

    # Execute $BROWSER with VS Code env vars
    proc_env = os.environ.copy()
    proc_env.update(env_vars)
    try:
        subprocess.Popen(
            [browser, final_url],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=proc_env,
        )
    except Exception as e:
        raise HTTPException(500, str(e))

    return {"ok": True}
