"""Read battery state."""

from physicar_agent import topic, text


def tool() -> list:
    """Read the robot's battery level.

    Returns percentage (0–100%) and voltage.
    Only available on real robot hardware.

    Examples:
        battery()
    """
    b = topic.get('/battery_state')
    if not b:
        return [text("battery: unavailable")]

    parts = []
    if 'percentage' in b:
        try:
            pct = float(b['percentage'])
            if pct <= 1.0:
                pct *= 100.0
            parts.append(f"{round(pct, 1)}%")
        except Exception:
            pass
    if 'voltage' in b:
        try:
            parts.append(f"{round(float(b['voltage']), 2)}V")
        except Exception:
            pass

    if not parts:
        return [text("battery: unavailable")]

    return [text("battery: " + " / ".join(parts))]
