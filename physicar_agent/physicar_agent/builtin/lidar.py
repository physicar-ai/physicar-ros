"""Read LiDAR scan data."""

import math
from typing import Annotated

from pydantic import Field

from physicar_agent import topic, text


def tool(
    step: Annotated[float, Field(
        description="Angular step in degrees (0.5~30, must be multiple of 0.5). "
                    "Native resolution is 0.5°. Default 5° gives ~72 points.",
        ge=0.5, le=30,
    )] = 5.0,
) -> list:
    """Read the latest 360° LiDAR scan.

    Down-samples by angular step. For invalid readings, substitutes the
    minimum valid neighbour within ±step/2.

    Orientation: 0°=front, +90°=left, -90°=right, ±180°=rear.
    Native resolution: 0.5° (~720 points). Range: 0.15–16 m.

    Point budget guide:
        step=0.5  → ~720 pts (full resolution)
        step=1    → ~360 pts
        step=3    → ~120 pts (default)
        step=5    → ~72 pts
        step=10   → ~36 pts

    Examples:
        lidar()              # 3° step, ~120 points
        lidar(step=1)        # 1° step, ~360 points
        lidar(step=10)       # 10° step, ~36 points
    """
    scan = topic.get('/scan')
    if not scan:
        return [text("lidar: unavailable")]

    ranges = scan.get('ranges') or []
    if not ranges:
        return [text("lidar: no data")]

    angle_min = float(scan.get('angle_min', 0.0))
    angle_inc = float(scan.get('angle_increment', 0.0))
    if angle_inc <= 0:
        return [text("lidar: invalid scan")]

    # Snap step to nearest 0.5° multiple
    step = round(step * 2) / 2
    step = max(0.5, min(step, 30.0))

    angle_inc_deg = math.degrees(angle_inc)
    range_min = float(scan.get('range_min', 0.0))
    range_max = float(scan.get('range_max', 0.0))
    n = len(ranges)

    def _is_valid(r):
        return r is not None and math.isfinite(r) and r > range_min and r < range_max

    def _min_neighbor(center, window_deg):
        w = int(window_deg / angle_inc_deg) + 1
        best = None
        for off in range(-w, w + 1):
            idx = center + off
            if 0 <= idx < n:
                r = ranges[idx]
                if _is_valid(r) and (best is None or r < best):
                    best = r
        return best

    out = []
    for i, r in enumerate(ranges):
        deg = math.degrees(angle_min + i * angle_inc)
        remainder = abs(deg % step)
        if remainder > 0.25 and remainder < (step - 0.25):
            continue
        angle_key = round(deg / step) * step
        angle_str = str(int(angle_key)) if angle_key == int(angle_key) else f"{angle_key:.1f}"

        if _is_valid(r):
            out.append(f"{angle_str}\u00b0={round(r, 3)}m")
        else:
            mn = _min_neighbor(i, step / 2)
            if mn is not None:
                out.append(f"{angle_str}\u00b0={round(mn, 3)}m")
            else:
                out.append(f"{angle_str}\u00b0=null")

    header = f"lidar (step={step}\u00b0, range={range_min}~{range_max}m, points={len(out)}):"
    return [text(f"{header}\n  {', '.join(out)}")]
