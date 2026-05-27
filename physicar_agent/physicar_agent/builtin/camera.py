"""Capture the latest camera image."""

import base64

from physicar_agent import topic, text, image


def tool() -> list:
    """Capture the latest camera frame as a JPEG image.

    Resolution: 480x360. Returns a single JPEG image.
    The camera is mounted on a pan/tilt gimbal controlled via
    the control tool (pan: ±45°, tilt: ±45°).

    Examples:
        camera()
    """
    msg = topic.raw('/camera/image_raw/compressed')
    if msg is None:
        return [text("camera: unavailable")]

    data = getattr(msg, 'data', None)
    if data is None:
        return [text("camera: unavailable")]

    try:
        b = bytes(data)
    except Exception:
        return [text("camera: unavailable")]

    if not b:
        return [text("camera: unavailable")]

    b64 = base64.b64encode(b).decode('ascii')
    return [image(b64)]
