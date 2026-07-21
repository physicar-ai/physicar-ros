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
Audio Manager — command-based playback engine (no ROS layer).

Every playback is a *sound instance* with an id:

    play(url=... | path=... | data=...)  -> {"id": ..., "duration": ...}
    stop(id) / stop_all() / set_volume(id, v) / status()

plus a realtime PCM16 path:

    stream_open(sample_rate, channels) -> id      (WS /audio/stream)
    stream_feed(id, frames) / stream_close(id)

Backends:
  - device: one mpv process per instance (decoding, buffering, network
    reconnect and live volume all handled by mpv; mixing by ALSA dmix).
    The PCM stream uses mpv's rawaudio demuxer fed via stdin.
  - sim: no speaker — commands are broadcast as SSE events
    (GET /audio/events) and the browser (gzweb) plays them; local files
    are exposed to the browser via GET /audio/file/{id}. PCM stream
    frames are relayed as base64 events.
"""

import asyncio
import base64
import json
import shutil
import socket
import subprocess
import threading
import time
import uuid
import wave
from pathlib import Path
from typing import Optional

from physicar_webserver.sim import is_sim_mode

CACHE_DIR = Path("/tmp/physicar-audio")

_MPV_BASE = [
    "mpv", "--no-video", "--no-terminal", "--really-quiet",
    "--audio-display=no", "--keep-open=no",
]


def _new_id() -> str:
    return uuid.uuid4().hex[:8]


class _Item:
    """One sound instance (a playback or a PCM stream)."""

    def __init__(self, kind: str, source: str, volume: float, loop: bool):
        self.id = _new_id()
        self.kind = kind          # url | path | data | stream
        self.source = source      # display string
        self.volume = volume
        self.loop = loop
        self.started = time.time()
        self.duration: Optional[float] = None
        # device backend
        self.proc: Optional[subprocess.Popen] = None
        self.ipc: Optional[str] = None
        # local file backing this item (data temp file, or a registered path)
        self.file: Optional[Path] = None
        self.tmp = False          # delete self.file on cleanup
        # stream params
        self.sample_rate = 0
        self.channels = 0


class AudioManager:
    def __init__(self):
        self.sim = None           # resolved lazily (ROS param not ready at import)
        self.items: dict[str, _Item] = {}
        self.lock = threading.Lock()
        # SSE subscribers: list of (asyncio.Queue, loop)
        self._subs: list = []
        CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # ── mode ────────────────────────────────────────────────────────────────
    def _is_sim(self) -> bool:
        if self.sim is None:
            try:
                self.sim = is_sim_mode()
            except Exception:
                return False
        return self.sim

    # ── SSE event bus ────────────────────────────────────────────────────────
    def subscribe(self):
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        loop = asyncio.get_event_loop()
        self._subs.append((q, loop))
        return q

    def unsubscribe(self, q):
        self._subs = [(sq, lp) for (sq, lp) in self._subs if sq is not q]

    def _broadcast(self, event: dict):
        for q, loop in list(self._subs):
            try:
                loop.call_soon_threadsafe(q.put_nowait, event)
            except Exception:
                pass

    # ── mpv helpers (device) ─────────────────────────────────────────────────
    def _mpv_ipc(self, item: _Item, *command):
        """Send one command over mpv's JSON IPC socket; returns response dict."""
        if not item.ipc:
            return None
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(1.0)
                s.connect(item.ipc)
                s.sendall(json.dumps({"command": list(command)}).encode() + b"\n")
                return json.loads(s.recv(65536).split(b"\n")[0])
        except Exception:
            return None

    def _mpv_prop(self, item: _Item, prop: str):
        resp = self._mpv_ipc(item, "get_property", prop)
        return resp.get("data") if resp and resp.get("error") == "success" else None

    def _spawn_mpv(self, item: _Item, src: str, extra: Optional[list] = None):
        item.ipc = str(CACHE_DIR / f"mpv-{item.id}.sock")
        cmd = _MPV_BASE + [
            f"--volume={int(max(0.0, min(1.0, item.volume)) * 100)}",
            f"--input-ipc-server={item.ipc}",
        ]
        if item.loop:
            cmd.append("--loop-file=inf")
        cmd += (extra or [])
        cmd.append(src)
        stdin = subprocess.PIPE if src == "-" else subprocess.DEVNULL
        item.proc = subprocess.Popen(
            cmd, stdin=stdin,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        threading.Thread(target=self._watch, args=(item,), daemon=True).start()

    def _watch(self, item: _Item):
        """Reap the mpv process and clean the item up when playback ends."""
        item.proc.wait()
        with self.lock:
            if self.items.get(item.id) is item:
                del self.items[item.id]
        self._cleanup_files(item)
        self._broadcast({"type": "ended", "id": item.id})

    def _cleanup_files(self, item: _Item):
        if item.tmp and item.file:
            try:
                item.file.unlink(missing_ok=True)
            except OSError:
                pass
        if item.ipc:
            try:
                Path(item.ipc).unlink(missing_ok=True)
            except OSError:
                pass

    # ── playback ─────────────────────────────────────────────────────────────
    def play(self, *, url: Optional[str] = None, path: Optional[str] = None,
             data: Optional[str] = None, fmt: str = "",
             volume: float = 1.0, loop: bool = False, replace: bool = False) -> dict:
        """Start one playback from exactly one source (url / path / data)."""
        if replace:
            self.stop_all()

        if url:
            item = _Item("url", url, volume, loop)
            src = url
        elif path:
            p = Path(path)
            if not p.is_file():
                raise FileNotFoundError(f"no such file: {path}")
            item = _Item("path", str(p), volume, loop)
            item.file = p
            src = str(p)
        elif data:
            raw = base64.b64decode(data)
            suffix = f".{fmt.lstrip('.')}" if fmt else ".mp3"
            item = _Item("data", f"<{len(raw)} bytes{suffix}>", volume, loop)
            item.file = CACHE_DIR / f"data-{item.id}{suffix}"
            item.file.write_bytes(raw)
            item.tmp = True
            src = str(item.file)
        else:
            raise ValueError("one of url / path / data is required")

        if item.file is not None:
            item.duration = self._probe_duration(item.file)

        with self.lock:
            self.items[item.id] = item

        if self._is_sim():
            self._broadcast({
                "type": "play", "id": item.id,
                "url": url if url else f"/audio/file/{item.id}",
                "volume": item.volume, "loop": item.loop,
            })
        else:
            self._spawn_mpv(item, src)

        return {"id": item.id, "duration": item.duration}

    def stop(self, item_id: str) -> bool:
        with self.lock:
            item = self.items.pop(item_id, None)
        if item is None:
            return False
        if item.proc and item.proc.poll() is None:
            item.proc.terminate()
        else:
            self._cleanup_files(item)
        self._broadcast({"type": "stop", "id": item_id})
        return True

    def stop_all(self):
        with self.lock:
            items = list(self.items.values())
            self.items.clear()
        for item in items:
            if item.proc and item.proc.poll() is None:
                item.proc.terminate()
            else:
                self._cleanup_files(item)
        self._broadcast({"type": "stop_all"})

    def set_duration(self, item_id: str, duration: float) -> bool:
        """The sim viewer reports the media duration once metadata loads.
        Without it a URL item can never be expired or skipped on SSE replay
        (the server itself never probes remote media)."""
        with self.lock:
            item = self.items.get(item_id)
            if item is None or item.kind == "stream" or not duration or duration <= 0:
                return False
            item.duration = float(duration)
        return True

    def set_volume(self, item_id: str, volume: float) -> bool:
        volume = max(0.0, min(1.0, volume))
        with self.lock:
            item = self.items.get(item_id)
        if item is None:
            return False
        item.volume = volume
        if self._is_sim():
            self._broadcast({"type": "volume", "id": item_id, "volume": volume})
        else:
            self._mpv_ipc(item, "set_property", "volume", int(volume * 100))
        return True

    def status(self) -> list:
        self._expire_sim_items()
        out = []
        with self.lock:
            items = list(self.items.values())
        for item in items:
            entry = {
                "id": item.id, "kind": item.kind, "source": item.source,
                "volume": item.volume, "loop": item.loop,
                "duration": item.duration,
                "position": round(time.time() - item.started, 1),
            }
            if not self._is_sim() and item.proc is not None:
                pos = self._mpv_prop(item, "time-pos")
                dur = self._mpv_prop(item, "duration")
                if pos is not None:
                    entry["position"] = round(pos, 1)
                if dur is not None:
                    entry["duration"] = round(dur, 1)
            out.append(entry)
        return out

    def _expire_sim_items(self):
        """sim: no process to reap — expire finished (non-loop) local items."""
        if not self._is_sim():
            return
        now = time.time()
        expired = []
        with self.lock:
            for item in list(self.items.values()):
                if (item.kind != "stream" and not item.loop
                        and item.duration is not None
                        and now - item.started > item.duration + 2.0):
                    expired.append(self.items.pop(item.id))
        for item in expired:
            self._cleanup_files(item)

    # ── realtime PCM16 stream (WS /audio/stream) ────────────────────────────
    def stream_open(self, sample_rate: int, channels: int, volume: float = 1.0) -> str:
        item = _Item("stream", f"pcm16 {sample_rate}Hz x{channels}", volume, False)
        item.sample_rate = sample_rate
        item.channels = channels
        with self.lock:
            self.items[item.id] = item
        if not self._is_sim():
            self._spawn_mpv(item, "-", extra=[
                "--demuxer=rawaudio",
                f"--demuxer-rawaudio-rate={sample_rate}",
                f"--demuxer-rawaudio-channels={channels}",
                "--demuxer-rawaudio-format=s16le",
                "--cache=no",
            ])
        return item.id

    def stream_feed(self, item_id: str, frames: bytes):
        with self.lock:
            item = self.items.get(item_id)
        if item is None:
            return
        if self._is_sim():
            self._broadcast({
                "type": "pcm", "id": item_id,
                "data": base64.b64encode(frames).decode(),
                "sample_rate": item.sample_rate, "channels": item.channels,
                "volume": item.volume,
            })
        elif item.proc and item.proc.poll() is None:
            try:
                item.proc.stdin.write(frames)
                item.proc.stdin.flush()
            except (BrokenPipeError, OSError):
                pass

    def stream_close(self, item_id: str):
        with self.lock:
            item = self.items.pop(item_id, None)
        if item is None:
            return
        if item.proc and item.proc.poll() is None:
            try:
                item.proc.stdin.close()   # let mpv drain the buffer, then exit
            except OSError:
                pass
        self._broadcast({"type": "stop", "id": item_id})

    # ── misc ─────────────────────────────────────────────────────────────────
    def file_for(self, item_id: str) -> Optional[Path]:
        """Local file backing an item (sim browser fetches it here)."""
        with self.lock:
            item = self.items.get(item_id)
        return item.file if item else None

    @staticmethod
    def _probe_duration(path: Path) -> Optional[float]:
        if path.suffix.lower() == ".wav":
            try:
                with wave.open(str(path), "rb") as w:
                    return round(w.getnframes() / w.getframerate(), 2)
            except Exception:
                pass
        if shutil.which("ffprobe"):
            try:
                out = subprocess.run(
                    ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
                     "-of", "csv=p=0", str(path)],
                    capture_output=True, text=True, timeout=5,
                )
                return round(float(out.stdout.strip()), 2)
            except Exception:
                pass
        return None


audio_manager = AudioManager()
