"""
PhysiCar Agent Core — HTTP-backed API

Provides a thin wrapper around the PhysiCar webserver REST API running
on localhost.  Tools use ``api.get()`` / ``api.post()`` to read sensor
state and send control commands.

Public API
----------
- api.get(path, **params) → dict | bytes
- api.post(path, **data)  → dict
- text(content)           → {"type": "text", "text": "..."}
- image(data, mime)       → {"type": "image", "mime": "...", "base64": "..."}
"""

import base64
import json
from typing import Any

import requests as _requests


# ============================================
# Response helper functions
# ============================================

def text(content) -> dict:
    """Create a text response object"""
    if isinstance(content, str):
        return {"type": "text", "text": content}
    elif isinstance(content, dict):
        return {"type": "text", "text": json.dumps(content, ensure_ascii=False)}
    else:
        return {"type": "text", "text": str(content)}


def image(data, mime: str = "image/jpeg") -> dict:
    """Create an image response object"""
    if isinstance(data, str):
        return {"type": "image", "mime": mime, "base64": data}

    if isinstance(data, bytes):
        return {"type": "image", "mime": mime, "base64": base64.b64encode(data).decode()}

    if hasattr(data, 'data') and hasattr(data, 'format'):
        fmt = data.format.lower() if data.format else 'jpeg'
        mime = f"image/{fmt}"
        return {"type": "image", "mime": mime, "base64": base64.b64encode(bytes(data.data)).decode()}

    try:
        from PIL import Image as PILImage
        if isinstance(data, PILImage.Image):
            import io
            buf = io.BytesIO()
            fmt = 'PNG' if data.mode == 'RGBA' else 'JPEG'
            data.save(buf, format=fmt)
            mime = f"image/{fmt.lower()}"
            return {"type": "image", "mime": mime, "base64": base64.b64encode(buf.getvalue()).decode()}
    except ImportError:
        pass

    try:
        import numpy as np
        if isinstance(data, np.ndarray):
            from PIL import Image as PILImage
            import io
            img = PILImage.fromarray(data)
            buf = io.BytesIO()
            fmt = 'PNG' if len(data.shape) > 2 and data.shape[2] == 4 else 'JPEG'
            img.save(buf, format=fmt)
            mime = f"image/{fmt.lower()}"
            return {"type": "image", "mime": mime, "base64": base64.b64encode(buf.getvalue()).decode()}
    except ImportError:
        pass

    raise ValueError(f"Unsupported image type: {type(data)}")


# ============================================
# HTTP API proxy
# ============================================

_BASE = "http://127.0.0.1"
_SESSION = _requests.Session()


class _Api:
    """Thin HTTP wrapper around the PhysiCar webserver.

    Usage::

        from physicar_agent import api

        odom = api.get('/state/odom')          # → dict
        jpeg = api.get('/state/camera')        # → bytes (JPEG)
        api.post('/control/speed', value=0.5)  # → dict
    """

    def get(self, path: str, **params) -> Any:
        """HTTP GET.  Returns dict (JSON) or bytes (image)."""
        r = _SESSION.get(f"{_BASE}{path}", params=params or None, timeout=5)
        r.raise_for_status()
        ct = r.headers.get("content-type", "")
        if ct.startswith("image/"):
            return r.content
        return r.json()

    def post(self, path: str, **data) -> dict:
        """HTTP POST (JSON body).  Returns response dict."""
        r = _SESSION.post(f"{_BASE}{path}", json=data, timeout=5)
        r.raise_for_status()
        return r.json()


api = _Api()
