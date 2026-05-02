"""Read motion-related state: speed, steering, heading, acceleration, pan/tilt."""

import math

from physicar_agent import topic, text


def _float64(name: str) -> float | None:
    v = topic.get(name)
    if not v:
        return None
    try:
        return float(v.get('data', 0.0))
    except Exception:
        return None


def tool() -> list:
    """Read the robot's current motion and IMU state.

    Returns:
        linear    — forward velocity x in m/s (from odometry)
        angular   — yaw rate z in rad/s (from odometry)
        steering  — front wheel angle in degrees
        heading   — IMU yaw (relative, not compass) in degrees
        accel     — IMU linear acceleration x/y/z in m/s²
        mag       — magnetometer x/y/z in µT (may be unavailable in sim)
        pan/tilt  — camera gimbal angles in degrees

    Examples:
        motion()
    """
    lines = []

    # Velocity from /odom
    odom = topic.get('/odom')
    if odom:
        try:
            tw = odom['twist']['twist']
            lx = round(float(tw['linear']['x']), 3)
            az = round(float(tw['angular']['z']), 3)
            lines.append(f"linear: {lx} m/s")
            lines.append(f"angular: {az} rad/s")
        except Exception:
            lines.append("linear: unavailable")
            lines.append("angular: unavailable")
    else:
        lines.append("linear: unavailable")
        lines.append("angular: unavailable")

    # Steering
    st = _float64('/steering')
    if st is not None:
        lines.append(f"steering: {round(math.degrees(st), 1)}\u00b0")
    else:
        lines.append("steering: unavailable")

    # IMU heading (yaw) and acceleration
    imu_data = topic.get('/imu')
    if imu_data:
        o = imu_data.get('orientation', {})
        # Quaternion to yaw
        x, y, z, w = o.get('x', 0), o.get('y', 0), o.get('z', 0), o.get('w', 1)
        siny = 2.0 * (w * z + x * y)
        cosy = 1.0 - 2.0 * (y * y + z * z)
        yaw = math.degrees(math.atan2(siny, cosy))
        lines.append(f"heading: {round(yaw, 1)}\u00b0")

        la = imu_data.get('linear_acceleration', {})
        ax = round(float(la.get('x', 0)), 2)
        ay = round(float(la.get('y', 0)), 2)
        az = round(float(la.get('z', 0)), 2)
        lines.append(f"accel: x={ax} y={ay} z={az} m/s\u00b2")
    else:
        lines.append("heading: unavailable")
        lines.append("accel: unavailable")

    # Magnetometer (ROS MagneticField is in Tesla → display as µT)
    mag_data = topic.get('/imu/mag')
    if mag_data:
        mf = mag_data.get('magnetic_field', {})
        mx = round(float(mf.get('x', 0)) * 1e6, 2)
        my = round(float(mf.get('y', 0)) * 1e6, 2)
        mz = round(float(mf.get('z', 0)) * 1e6, 2)
        lines.append(f"mag: x={mx} y={my} z={mz} \u00b5T")
    else:
        lines.append("mag: unavailable")

    # Pan / Tilt
    pan = _float64('/camera/pan')
    if pan is not None:
        lines.append(f"pan: {round(math.degrees(pan), 1)}\u00b0")
    else:
        lines.append("pan: unavailable")

    tilt = _float64('/camera/tilt')
    if tilt is not None:
        lines.append(f"tilt: {round(math.degrees(tilt), 1)}\u00b0")
    else:
        lines.append("tilt: unavailable")

    return [text("\n".join(lines))]
