#!/usr/bin/env python3
"""
Host API — lightweight host-side service for VS Code / Codespaces features.

Runs on port 8001, proxied by nginx at /api/host/.
Provides machine type info and change for GitHub Codespaces.
Also provides /open-external to open URLs in the host browser via $BROWSER.
"""

import json
import os
import subprocess
from pathlib import Path

import requests
from flask import Flask, jsonify, request

app = Flask(__name__)

CODESPACE_NAME = os.environ.get("CODESPACE_NAME", "")


@app.route("/machines")
def get_machines():
    """List available machine types + current machine info."""
    if not CODESPACE_NAME:
        return jsonify(error="Not running in Codespace"), 400

    try:
        # Current codespace info
        cs_result = subprocess.run(
            ["gh", "api", f"user/codespaces/{CODESPACE_NAME}",
             "--jq", "{machine: .machine}"],
            capture_output=True, text=True, timeout=30,
        )
        if cs_result.returncode != 0:
            return jsonify(error=cs_result.stderr.strip()), 502

        cs = json.loads(cs_result.stdout)
        current_machine = cs.get("machine", {})

        # Available machine types
        machines_result = subprocess.run(
            ["gh", "api", f"user/codespaces/{CODESPACE_NAME}/machines",
             "--jq", ".machines[] | {name, display_name, cpus, memory_gb: (.memory_in_bytes/1073741824), storage_gb: (.storage_in_bytes/1073741824)}"],
            capture_output=True, text=True, timeout=30,
        )
        if machines_result.returncode != 0:
            return jsonify(error=machines_result.stderr.strip()), 502

        machines = []
        for line in machines_result.stdout.strip().split("\n"):
            if line:
                machines.append(json.loads(line))

        # Billable owner
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

        return jsonify(
            machines=machines,
            current=current_machine.get("name", ""),
            cpus=current_machine.get("cpus", 0),
            memory_gb=int(current_machine.get("memory_in_bytes", 0) / 1073741824),
            storage_gb=int(current_machine.get("storage_in_bytes", 0) / 1073741824),
            billable_owner=billable_owner,
        )
    except subprocess.TimeoutExpired:
        return jsonify(error="GitHub API timeout"), 504
    except Exception as e:
        return jsonify(error=str(e)), 500


@app.route("/machine", methods=["POST"])
def change_machine():
    """Change Codespace machine type and restart."""
    if not CODESPACE_NAME:
        return jsonify(error="Not running in Codespace"), 400

    data = request.get_json(silent=True) or {}
    machine_name = data.get("machine", "").strip()
    if not machine_name:
        return jsonify(error="machine required"), 400

    try:
        # Change machine type
        edit = subprocess.run(
            ["gh", "codespace", "edit", "-c", CODESPACE_NAME, "-m", machine_name],
            capture_output=True, text=True, timeout=60,
        )
        if edit.returncode != 0:
            return jsonify(error=edit.stderr.strip()), 500

        # Restart codespace with new machine
        subprocess.run(
            ["gh", "api", "--method", "POST",
             f"user/codespaces/{CODESPACE_NAME}/start"],
            capture_output=True, text=True, timeout=60,
        )

        return jsonify(success=True, machine=machine_name)
    except subprocess.TimeoutExpired:
        return jsonify(error="GitHub API timeout"), 504
    except Exception as e:
        return jsonify(error=str(e)), 500


@app.route("/open-external")
def open_external():
    """Open a URL in the host browser using VS Code's $BROWSER.
    Follows redirects to get final URL (avoids untrusted domain prompts)."""
    url = request.args.get("url", "").strip()
    if not url:
        return jsonify(ok=False, error="url required"), 400

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

    # Follow redirects to get final URL (e.g. github.com)
    final_url = url
    try:
        r = requests.head(url, allow_redirects=False, timeout=5)
        if r.status_code in (301, 302, 303, 307, 308):
            final_url = r.headers.get("Location", url)
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
        return jsonify(ok=False, error=str(e)), 500

    return jsonify(ok=True)


# ── MyApp service control ──────────────────────────────────────────

_MYAPP_ALLOWED_ACTIONS = {"start", "stop", "restart"}
_MYAPP_SYSTEMD_UNIT = "physicar-myapp.service"
_MYAPP_SUPERVISOR_PROGRAM = "myapp"


@app.route("/myapp/service", methods=["POST"])
def myapp_service():
    """Start / stop / restart the myapp host service.

    On device: systemctl <action> physicar-myapp.service
    On SIM:    supervisorctl <action> myapp
    """
    data = request.get_json(silent=True) or {}
    action = data.get("action", "").strip()
    if action not in _MYAPP_ALLOWED_ACTIONS:
        return jsonify(ok=False, error="invalid action"), 400

    if CODESPACE_NAME:
        cmd = ["supervisorctl", "-s", "unix:///tmp/supervisor.sock",
               action, _MYAPP_SUPERVISOR_PROGRAM]
    else:
        cmd = ["systemctl", action, _MYAPP_SYSTEMD_UNIT]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            return jsonify(ok=False, error=result.stderr.strip()), 500
        return jsonify(ok=True)
    except subprocess.TimeoutExpired:
        return jsonify(ok=False, error="timeout"), 504
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8001)
