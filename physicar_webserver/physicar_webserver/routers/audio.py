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
Audio router — command-based playback (see audio_manager.py).

    POST /audio/play    {url | path | data(+format)} + volume/loop/replace
    POST /audio/stop    {id} or {all: true}
    POST /audio/volume  {id, volume}
    GET  /audio         current playback list
    GET  /audio/events  SSE command relay (sim browser playback / UI display)
    GET  /audio/file/{id}   local file backing an item (sim browser fetch)
    WS   /audio/stream  realtime PCM16 (binary frames; close = stop)
"""

import asyncio
import json
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from physicar_webserver.audio_manager import audio_manager

router = APIRouter(tags=["Audio"])


class PlayRequest(BaseModel):
    url: Optional[str] = Field(default=None, description="Remote audio URL (http/https)")
    path: Optional[str] = Field(default=None, description="Local file path on the robot")
    data: Optional[str] = Field(default=None, description="Base64-encoded audio file (mp3/wav/ogg/...) — played once, not stored")
    format: str = Field(default="", description="Container hint for `data` (default mp3)")
    volume: float = Field(default=1.0, ge=0.0, le=1.0)
    loop: bool = Field(default=False)
    replace: bool = Field(default=False, description="Stop everything currently playing first")


class StopRequest(BaseModel):
    id: Optional[str] = None
    all: bool = False


class DurationRequest(BaseModel):
    id: str
    duration: float = Field(gt=0)


class VolumeRequest(BaseModel):
    id: str
    volume: float = Field(ge=0.0, le=1.0)


@router.post("/audio/play")
async def play(req: PlayRequest):
    sources = [s for s in (req.url, req.path, req.data) if s]
    if len(sources) != 1:
        raise HTTPException(400, "exactly one of url / path / data is required")
    try:
        result = audio_manager.play(
            url=req.url, path=req.path, data=req.data, fmt=req.format,
            volume=req.volume, loop=req.loop, replace=req.replace,
        )
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(400, str(e))
    return {"success": True, **result}


@router.post("/audio/stop")
async def stop(req: StopRequest):
    if req.all:
        audio_manager.stop_all()
        return {"success": True}
    if not req.id:
        raise HTTPException(400, "id or all is required")
    if not audio_manager.stop(req.id):
        raise HTTPException(404, f"no such sound: {req.id}")
    return {"success": True}


@router.post("/audio/volume")
async def volume(req: VolumeRequest):
    if not audio_manager.set_volume(req.id, req.volume):
        raise HTTPException(404, f"no such sound: {req.id}")
    return {"success": True, "volume": req.volume}


@router.post("/audio/duration")
async def duration(req: DurationRequest):
    """The sim viewer reports media duration (metadata) — lets the server
    expire finished URL items instead of replaying them to the next viewer."""
    if not audio_manager.set_duration(req.id, req.duration):
        raise HTTPException(404, f"no such sound: {req.id}")
    return {"success": True}


@router.get("/audio")
async def status():
    return {"playing": audio_manager.status()}


@router.get("/audio/file/{item_id}")
async def audio_file(item_id: str):
    f = audio_manager.file_for(item_id)
    if f is None or not f.is_file():
        raise HTTPException(404, "no file for this sound")
    return FileResponse(str(f))


@router.get("/audio/events")
async def events(request: Request):
    """SSE stream of playback commands — the sim browser plays from this."""
    q = audio_manager.subscribe()

    async def gen():
        try:
            # replay current state so a late-joining browser starts playing.
            # 끝난 곡 재재생 사고 방지(리스폰=페이지 리로드→SSE 재구독):
            #  - duration 을 아는 비루프 곡이 이미 끝났으면 건너뛴다
            #  - 재생 중인 곡은 offset(경과초)을 실어 이어듣게 한다
            #  - URL 곡은 duration 미상 → 브라우저의 자연 종료 통지(POST /audio/stop)가
            #    서버 보관 상태를 지우는 유일한 수단 (gzweb.js onended 참고)
            for item in audio_manager.status():
                if item["kind"] not in ("url", "path", "data"):
                    continue
                pos = item.get("position") or 0.0
                dur = item.get("duration")
                if not item["loop"] and dur is not None and pos > dur:
                    continue
                offset = (pos % dur) if (item["loop"] and dur) else pos
                yield "data: " + json.dumps({
                    "type": "play", "id": item["id"],
                    "url": item["source"] if item["kind"] == "url" else f"/audio/file/{item['id']}",
                    "volume": item["volume"], "loop": item["loop"],
                    "offset": round(offset, 2),
                }) + "\n\n"
            while True:
                if await request.is_disconnected():
                    return
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield "data: " + json.dumps(ev) + "\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            audio_manager.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.websocket("/audio/stream")
async def stream(ws: WebSocket):
    """Realtime PCM16 playback: binary frames in, close = stop.

    Query params: sample_rate (default 24000 — OpenAI Realtime), channels
    (default 1), volume (0..1). A JSON text frame {"volume": v} adjusts
    volume mid-stream.
    """
    await ws.accept()
    sample_rate = int(ws.query_params.get("sample_rate", 24000))
    channels = int(ws.query_params.get("channels", 1))
    vol = float(ws.query_params.get("volume", 1.0))

    item_id = audio_manager.stream_open(sample_rate, channels, vol)
    await ws.send_json({"id": item_id, "sample_rate": sample_rate, "channels": channels})

    loop = asyncio.get_event_loop()
    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            if msg.get("bytes"):
                # feed in a worker thread — device backend writes to mpv stdin
                await loop.run_in_executor(None, audio_manager.stream_feed, item_id, msg["bytes"])
            elif msg.get("text"):
                try:
                    cmd = json.loads(msg["text"])
                    if "volume" in cmd:
                        audio_manager.set_volume(item_id, float(cmd["volume"]))
                except (ValueError, TypeError):
                    pass
    except WebSocketDisconnect:
        pass
    finally:
        audio_manager.stream_close(item_id)
