#!/usr/bin/env python3
#
# Copyright 2026 AICASTLE Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");

"""
Run commands in the host's namespace from inside the container.

Container is started with ``--privileged --pid host``, so PID 1 is the
host's systemd.  ``nsenter -t 1 -m -u -i -n -p`` enters all of the host's
namespaces and runs the given command as host root.
"""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass
from typing import Sequence


_NSENTER = ["nsenter", "-t", "1", "-m", "-u", "-i", "-n", "-p", "--"]


@dataclass
class HostResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class HostExecError(RuntimeError):
    def __init__(self, result: HostResult, cmd: Sequence[str]):
        super().__init__(
            f"host command failed (rc={result.returncode}): "
            f"{shlex.join(cmd)}\n{result.stderr.strip()}"
        )
        self.result = result
        self.cmd = list(cmd)


def host_run(
    cmd: Sequence[str],
    *,
    timeout: float = 10.0,
    check: bool = False,
) -> HostResult:
    """Run ``cmd`` in the host's namespace and capture output."""
    full = [*_NSENTER, *cmd]
    try:
        proc = subprocess.run(
            full,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as e:
        raise HostExecError(
            HostResult(127, "", f"nsenter not available: {e}"), cmd
        ) from e
    except subprocess.TimeoutExpired as e:
        raise HostExecError(
            HostResult(124, "", f"timeout after {timeout}s"), cmd
        ) from e
    res = HostResult(proc.returncode, proc.stdout, proc.stderr)
    if check and not res.ok:
        raise HostExecError(res, cmd)
    return res


def systemctl(
    action: str, unit: str, *, timeout: float = 10.0, check: bool = True
) -> HostResult:
    """Convenience wrapper for ``systemctl <action> <unit>`` on the host."""
    return host_run(["systemctl", action, unit], timeout=timeout, check=check)


def supervisorctl(
    action: str, program: str, *, timeout: float = 10.0, check: bool = True
) -> HostResult:
    """Run ``supervisorctl <action> <program>`` on the host via nsenter.

    In SIM mode supervisord runs on the host (as the ``physicar`` user),
    while the webserver runs inside the container, so we still need to
    enter the host's namespaces.  nsenter lands us as root in the host,
    which can talk to any user's supervisord socket.
    """
    return host_run(
        ["supervisorctl", "-s", "unix:///tmp/supervisor.sock", action, program],
        timeout=timeout, check=check,
    )


def service_control(
    action: str,
    *,
    systemd_unit: str,
    supervisor_program: str,
    timeout: float = 10.0,
    check: bool = True,
) -> HostResult:
    """Control a host service using whichever supervisor is in charge.

    - Real device: ``systemctl <action> <unit>`` via nsenter (PID 1 = systemd)
    - SIM (codespaces): ``supervisorctl <action> <program>`` via nsenter
      (host runs supervisord as the ``physicar`` user)
    """
    from .sim import is_sim_mode

    if is_sim_mode():
        return supervisorctl(
            action, supervisor_program, timeout=timeout, check=check
        )
    return systemctl(action, systemd_unit, timeout=timeout, check=check)
