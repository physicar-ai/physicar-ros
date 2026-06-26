"""
Shared UI state across all open browsers.

Currently tracks the active navigation tab (e.g. 'control', 'agent',
'deepracer'). When one client changes tab, all other connected clients
receive the update via SSE and switch automatically.

Endpoints:
    GET  /uistate           - current state (one-shot JSON)
    GET  /uistate?stream=true - SSE stream (push on change)
    POST /uistate/tab       - set active tab, broadcasts to all
"""
import asyncio
import json
import time
from typing import Optional, Set

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

router = APIRouter(prefix="/uistate", tags=["uistate"])


class _State:
    def __init__(self):
        self.tab: str = "control"
        self.updated_at: float = time.time()


_state = _State()
_subscribers: Set[asyncio.Queue] = set()


def _payload() -> str:
    return json.dumps({"tab": _state.tab, "updated_at": _state.updated_at})


def _broadcast():
    msg = _payload()
    for q in list(_subscribers):
        try:
            q.put_nowait(msg)
        except asyncio.QueueFull:
            pass


class TabRequest(BaseModel):
    tab: str = Field(..., description="Tab id (e.g. 'control', 'agent', 'deepracer')")


@router.get("")
async def get_state(
    request: Request,
    stream: Optional[bool] = Query(None),
):
    accept = request.headers.get("accept", "")
    if stream or "text/event-stream" in accept:
        return StreamingResponse(
            _stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    return {"tab": _state.tab, "updated_at": _state.updated_at}


async def _stream():
    q: asyncio.Queue = asyncio.Queue(maxsize=8)
    _subscribers.add(q)
    try:
        # Send current state immediately
        yield f"data: {_payload()}\n\n"
        while True:
            msg = await q.get()
            yield f"data: {msg}\n\n"
    except asyncio.CancelledError:
        pass
    finally:
        _subscribers.discard(q)


@router.post("/tab")
async def set_tab(req: TabRequest):
    if req.tab and req.tab != _state.tab:
        _state.tab = req.tab
        _state.updated_at = time.time()
        _broadcast()
    return {"tab": _state.tab, "updated_at": _state.updated_at}
