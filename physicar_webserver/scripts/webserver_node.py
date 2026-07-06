#!/usr/bin/env python3
#
# Copyright 2026 AICASTLE Inc.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""
PhysiCar WebServer Node - ROS2 Node that runs FastAPI server

Runs uvicorn in-process (background thread) with ROS2 node lifecycle.

DEV Mode (DEV=true in /opt/physicar/userdata/.env):
    Watches source files for changes and restarts the process via os.execv().
    No subprocess, no zombie — clean self-replacement.
"""

import os
import sys
import threading
import rclpy
from rclpy.node import Node
import uvicorn

from physicar_webserver.main import app
from physicar_webserver.ros_bridge import get_ros_bridge


def is_dev_mode() -> bool:
    """Check if DEV mode is enabled via /opt/physicar/userdata/.env"""
    env_file = '/opt/physicar/userdata/.env'
    if not os.path.exists(env_file):
        return False
    try:
        with open(env_file, 'r') as f:
            for line in f:
                line = line.strip()
                if line.startswith('#') or '=' not in line:
                    continue
                key, value = line.split('=', 1)
                if key.strip() == 'DEV' and value.strip().lower() in ('true', '1', 'yes', 'on'):
                    return True
    except Exception:
        pass
    return False


def _start_file_watcher(node: Node, watch_dir: str):
    """Watch Python files for changes; restart process via os.execv() on change."""
    try:
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler
    except ImportError:
        node.get_logger().warn('[DEV] watchdog not installed — auto-reload disabled')
        return

    class _ReloadHandler(FileSystemEventHandler):
        def __init__(self):
            self._timer = None

        def _schedule_restart(self):
            """Debounce: wait 1s after last change before restarting."""
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(1.0, self._do_restart)
            self._timer.daemon = True
            self._timer.start()

        def _do_restart(self):
            node.get_logger().info('[DEV] File change detected — restarting...')
            # Clean shutdown: stop uvicorn first to release port
            try:
                node.shutdown_server()
            except Exception:
                pass
            try:
                get_ros_bridge().shutdown()
                node.destroy_node()
                rclpy.shutdown()
            except Exception:
                pass
            # Replace current process with fresh one
            os.execv(sys.executable, [sys.executable] + sys.argv)

        def on_modified(self, event):
            if event.src_path.endswith('.py'):
                self._schedule_restart()

        def on_created(self, event):
            if event.src_path.endswith('.py'):
                self._schedule_restart()

    observer = Observer()
    observer.schedule(_ReloadHandler(), watch_dir, recursive=True)
    observer.daemon = True
    observer.start()
    node.get_logger().info(f'[DEV] Watching for changes in {watch_dir}')


class WebServerNode(Node):
    """ROS2 Node that runs the FastAPI webserver."""

    def __init__(self):
        super().__init__('webserver')

        # Initialize ROS bridge
        bridge = get_ros_bridge()
        bridge.init(self)

        # Start uvicorn in background thread (same process, no subprocess)
        self._server = None
        self._server_thread = threading.Thread(
            target=self._run_server,
            daemon=True
        )
        self._server_thread.start()

        # DEV mode: watch source files for auto-restart
        self._dev_mode = is_dev_mode()
        if self._dev_mode:
            from pathlib import Path
            watch_dir = str(Path(__file__).resolve().parent.parent)
            _start_file_watcher(self, watch_dir)

        mode_str = '[DEV auto-reload]' if self._dev_mode else '[Production]'
        self.get_logger().info(f'WebServer started on http://127.0.0.1:8000 {mode_str}')

    def _run_server(self):
        """Run uvicorn in background thread."""
        config = uvicorn.Config(
            app,
            host='127.0.0.1',
            port=8000,
            log_level='info',
            access_log=False,
        )
        self._server = uvicorn.Server(config)
        self._server.run()

    def shutdown_server(self):
        """Signal uvicorn to stop and wait for socket release."""
        if self._server:
            self._server.should_exit = True
            self._server_thread.join(timeout=3.0)


def main(args=None):
    rclpy.init(args=args)
    node = WebServerNode()

    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.get_logger().info('Shutting down...')
        node.shutdown_server()
        get_ros_bridge().shutdown()
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
