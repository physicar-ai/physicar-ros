"""Read the robot's current state (sensors and actuators)."""

import base64
import math
from typing import Annotated

from pydantic import Field

from physicar_agent import topic, service, action, text, image


_VALID = {"camera", "lidar", "imu", "speed", "steering", "pan", "tilt", "battery"}


def _camera_jpeg_b64() -> str | None:
    """Return the latest /camera/image_raw/compressed JPEG as a base64 string, or None."""
    msg = topic.raw('/camera/image_raw/compressed')
    if msg is None:
        return None
    data = getattr(msg, 'data', None)
    if data is None:
        return None
    # Handle array.array('B'), list, and bytes uniformly.
    try:
        b = bytes(data)
    except Exception:
        return None
    if not b:
        return None
    return base64.b64encode(b).decode('ascii')


def _lidar_summary(step_deg: float = 5.0) -> dict | None:
    """Down-sample /scan ranges by step_deg degrees and return them."""
    scan = topic.get('/scan')
    if not scan:
        return None
    ranges = scan.get('ranges') or []
    if not ranges:
        return None
    angle_min = float(scan.get('angle_min', 0.0))
    angle_inc = float(scan.get('angle_increment', 0.0))
    if angle_inc <= 0:
        return None
    step = max(1, int(round(math.radians(step_deg) / angle_inc)))
    out = []
    for i in range(0, len(ranges), step):
        r = ranges[i]
        if r is None or (isinstance(r, float) and (math.isnan(r) or math.isinf(r))):
            continue
        deg = math.degrees(angle_min + i * angle_inc)
        out.append({"angle_deg": round(deg, 1), "range_m": round(float(r), 2)})
    return {
        "step_deg": step_deg,
        "range_min_m": float(scan.get('range_min', 0.0)),
        "range_max_m": float(scan.get('range_max', 0.0)),
        "samples": out,
    }


def _speed_mps() -> float | None:
    odom = topic.get('/odom')
    if not odom:
        return None
    try:
        return round(float(odom['twist']['twist']['linear']['x']), 3)
    except Exception:
        return None


def _float64(name: str) -> float | None:
    """Return the `data` field of a Float64 topic."""
    v = topic.get(name)
    if not v:
        return None
    try:
        return float(v.get('data', 0.0))
    except Exception:
        return None


def _battery() -> dict | None:
    b = topic.get('/battery_state')
    if not b:
        return None
    out = {}
    if 'percentage' in b:
        try:
            pct = float(b['percentage'])
            # ROS standard is 0.0..1.0; some drivers publish 0..100 instead.
            if pct <= 1.0:
                pct *= 100.0
            out['percentage'] = round(pct, 1)
        except Exception:
            pass
    if 'voltage' in b:
        try:
            out['voltage'] = round(float(b['voltage']), 2)
        except Exception:
            pass
    return out or None


def tool(
    include: Annotated[list, Field(
        description="Which items to fetch. Allowed: camera, lidar, imu, speed, steering, pan, tilt, battery"
    )]
) -> list:
    """Read the robot's current sensor and actuator state.

    Pass only the items you actually need. Including "camera" returns the
    latest JPEG frame alongside the text fields.

    Examples:
        state(include=["speed", "battery"])
        state(include=["camera"])
        state(include=["camera", "lidar"])
    """
    items = [x for x in (include or []) if x in _VALID]
    if not items:
        return [{"type": "text", "text": "no items requested. valid: " + ", ".join(sorted(_VALID))}]

    text_lines = []
    image_content = None

    for it in items:
        if it == "camera":
            b64 = _camera_jpeg_b64()
            if b64:
                image_content = {"type": "image", "mime": "image/jpeg", "base64": b64}
            else:
                text_lines.append("camera: unavailable")

        elif it == "lidar":
            ld = _lidar_summary()
            if ld:
                # Compact text representation: angle=range pairs in one line.
                samples = ", ".join(f"{s['angle_deg']}°={s['range_m']}m" for s in ld['samples'])
                text_lines.append(
                    f"lidar (step={ld['step_deg']}°, range={ld['range_min_m']}~{ld['range_max_m']}m):\n  {samples}"
                )
            else:
                text_lines.append("lidar: unavailable")

        elif it == "imu":
            imu_data = topic.get('/imu')
            if imu_data:
                o = imu_data.get('orientation', {})
                av = imu_data.get('angular_velocity', {})
                la = imu_data.get('linear_acceleration', {})
                text_lines.append(
                    f"imu orientation: x={o.get('x',0):.4f} y={o.get('y',0):.4f} z={o.get('z',0):.4f} w={o.get('w',0):.4f}\n"
                    f"imu angular_velocity: x={av.get('x',0):.4f} y={av.get('y',0):.4f} z={av.get('z',0):.4f}\n"
                    f"imu linear_acceleration: x={la.get('x',0):.2f} y={la.get('y',0):.2f} z={la.get('z',0):.2f}"
                )
            else:
                text_lines.append("imu: unavailable")

        elif it == "speed":
            v = _speed_mps()
            text_lines.append(f"speed: {v} m/s" if v is not None else "speed: unavailable")

        elif it == "steering":
            v = _float64('/steering')
            if v is None:
                text_lines.append("steering: unavailable")
            else:
                text_lines.append(f"steering: {round(math.degrees(v), 1)}°")

        elif it == "pan":
            v = _float64('/camera/pan')
            if v is None:
                text_lines.append("pan: unavailable")
            else:
                text_lines.append(f"pan: {round(math.degrees(v), 1)}°")

        elif it == "tilt":
            v = _float64('/camera/tilt')
            if v is None:
                text_lines.append("tilt: unavailable")
            else:
                text_lines.append(f"tilt: {round(math.degrees(v), 1)}°")

        elif it == "battery":
            bat = _battery()
            if bat:
                parts = []
                if 'percentage' in bat:
                    parts.append(f"{bat['percentage']}%")
                if 'voltage' in bat:
                    parts.append(f"{bat['voltage']}V")
                text_lines.append("battery: " + " / ".join(parts))
            else:
                text_lines.append("battery: unavailable")

    contents: list = []
    if text_lines:
        contents.append(text("\n".join(text_lines)))
    if image_content is not None:
        contents.append(image_content)
    return contents
