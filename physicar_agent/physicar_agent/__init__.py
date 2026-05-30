"""PhysiCar Agent — HTTP-backed Tool API for LLM agents"""

from .core import (
    api,        # api.get('/state/odom'), api.post('/control/speed', value=0.5)
    text,       # text(content) → {"type": "text", "text": "..."}
    image,      # image(data) → {"type": "image", "mime": "...", "base64": "..."}
)

__all__ = [
    'api',
    'text',
    'image',
]
