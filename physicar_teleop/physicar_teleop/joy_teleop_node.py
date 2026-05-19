"""Gamepad teleop for PhysiCar.

Subscribes to /joy (sensor_msgs/Joy from ros-jazzy-joy, which uses SDL2
under the hood so axes/buttons are normalised across Xbox, PlayStation,
Switch Pro, etc.) and publishes to a *separate* set of teleop topics so
the driver can pick which input source to apply based on lock state:

    /teleop/speed         std_msgs/Float64   m/s
    /teleop/steering      std_msgs/Float64   rad
    /teleop/camera/pan    std_msgs/Float64   rad
    /teleop/camera/tilt   std_msgs/Float64   rad

The driver subscribes to BOTH the public `/speed`,`/steering`,`/camera/*`
and these `/teleop/*` mirrors, then uses the latched
`/physicar_joy_teleop/status` topic (drive_engaged / camera_engaged fields) to
decide which one to forward to hardware.  This way deepracer/agent/REST
keep publishing freely on the public topics; their messages are simply
ignored by the driver while the user is holding LB/RB, and resume the
moment the buttons are released — perfect for "manual rescue back to the
track while autonomous keeps running" scenarios.

Press-and-hold semantics:

    LB (deadman_button)        - while held, joy_teleop drives /speed and
                                 /steering AND advertises drive_engaged=true so
                                 other publishers (REST /control, deepracer,
                                 agent) yield.  Releasing emits a single zero
                                 frame and clears drive_engaged.
    RB (camera_assist_button)  - while held, joy_teleop integrates the right
                                 stick into camera pan/tilt AND advertises
                                 camera_engaged=true so other publishers yield.

Runtime control:

    Parameter  enabled : bool  - when false, this node publishes nothing and
                                 status.enabled is false.  Toggle with
                                 `ros2 param set /physicar_joy_teleop enabled false`
                                 or via REST POST /joy/enabled.
    Service    ~/get_mapping   - dump current axis/button/limit mapping
    Service    ~/set_mapping   - update mapping (single key or bulk JSON);
                                 save_to_file=true persists to
                                 /home/physicar/physicar_ws/userdata/joy_mapping.json so it survives
                                 reboots and overrides the YAML defaults.
    Topic      ~/status        - latched (TRANSIENT_LOCAL) JoyTeleopStatus so
                                 late subscribers see the current locks.
"""
from __future__ import annotations

import json
import math
import os
from typing import Any

import rclpy
from rclpy.node import Node
from rclpy.parameter import Parameter
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from rcl_interfaces.msg import SetParametersResult
from sensor_msgs.msg import Joy
from std_msgs.msg import Float64

from physicar_interfaces.msg import JoyTeleopStatus, TeleopStatus
from physicar_interfaces.srv import GetJoyMapping, SetJoyMapping

from builtin_interfaces.msg import Duration as DurationMsg


MAPPING_FILE = '/home/physicar/physicar_ws/userdata/joy_mapping.json'

# Type registry — drives both validation and which request field to read.
_INT_KEYS = (
    'axis_speed', 'axis_steering', 'axis_pan', 'axis_tilt',
    'deadman_button', 'estop_button', 'center_camera_button',
    'camera_assist_button',
)
_FLOAT_KEYS = (
    'max_speed', 'max_steering', 'max_pan', 'max_tilt', 'deadzone', 'rate',
)
_BOOL_KEYS = (
    'invert_speed', 'invert_steering', 'invert_pan', 'invert_tilt',
)
_ALL_KEYS = _INT_KEYS + _FLOAT_KEYS + _BOOL_KEYS


def _clip(v: float, lim: float) -> float:
    if v > lim:
        return lim
    if v < -lim:
        return -lim
    return v


class JoyTeleopNode(Node):
    def __init__(self) -> None:
        super().__init__('physicar_joy_teleop')

        # ── Parameter defaults (also act as fallback when JSON missing) ──
        defaults = [
            ('max_speed', 2.0),         # m/s
            ('max_steering', 26.0),     # deg (wheel angle)
            ('max_pan', 45.0),          # deg
            ('max_tilt', 45.0),         # deg
            ('axis_speed', 1),
            ('axis_steering', 0),
            ('axis_pan', 3),
            ('axis_tilt', 4),
            ('invert_speed', False),
            ('invert_steering', False),
            ('invert_pan', False),
            ('invert_tilt', False),
            ('deadzone', 0.05),
            ('deadman_button', 4),          # LB
            ('camera_assist_button', 5),    # RB
            ('estop_button', 1),            # B
            ('center_camera_button', 7),    # Start
            ('rate', 30.0),
        ]
        params = self.declare_parameters(namespace='', parameters=defaults)
        self._mapping: dict[str, Any] = {p.name: p.value for p in params}
        self._mapping_source = 'default'
        self._mapping_units = 'degrees'  # current saved units (file may override)

        # If a persisted file exists it overrides yaml defaults.
        self._load_mapping_from_file()
        self._migrate_legacy_radians()

        # ── Runtime enable flag (separate parameter so it can be toggled
        # without touching the mapping file) ──
        self.declare_parameter('enabled', True)
        self._enabled: bool = bool(self.get_parameter('enabled').value)
        self.add_on_set_parameters_callback(self._on_param_set)

        # ── Publishers ──
        # NOTE: We deliberately publish to a /teleop/* mirror namespace
        # rather than /speed,/steering,/camera/* directly.  The driver
        # subscribes to both and gates by drive_engaged/camera_engaged so the
        # public topics keep flowing from deepracer / REST and only get
        # *applied* when the user releases LB/RB.
        self.pub_speed = self.create_publisher(Float64, '/teleop/speed', 10)
        self.pub_steer = self.create_publisher(Float64, '/teleop/steering', 10)
        self.pub_pan = self.create_publisher(Float64, '/teleop/camera/pan', 10)
        self.pub_tilt = self.create_publisher(Float64, '/teleop/camera/tilt', 10)

        # Status — split into two streams:
        #   /physicar_joy_teleop/status  (JoyTeleopStatus, joy-only fields)
        #   /teleop/status               (TeleopStatus, source-agnostic locks)
        #
        # JoyTeleopStatus carries only sticky configuration (currently
        # `enabled`) so we LATCH it (TRANSIENT_LOCAL) — late subscribers
        # immediately learn the current state without us having to spam
        # the topic at 30Hz.  This is safe because it carries no lock
        # state; lock state lives in TeleopStatus and is gated by
        # freshness, so a crashed joy_teleop can't keep the driver gated.
        joy_status_qos = QoSProfile(
            depth=1,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
        )
        self.pub_status = self.create_publisher(
            JoyTeleopStatus, '~/status', joy_status_qos
        )
        # TeleopStatus is republished every tick (~30Hz) with plain
        # RELIABLE+KEEP_LAST(1).  No TRANSIENT_LOCAL: consumers rely on
        # freshness (each message carries its own `timeout`) so a teleop
        # publisher that crashes while holding a lock can't leave the
        # driver permanently gated — the locks auto-expire.
        teleop_status_qos = QoSProfile(
            depth=1,
            reliability=ReliabilityPolicy.RELIABLE,
        )
        self.pub_teleop_status = self.create_publisher(
            TeleopStatus, '/teleop/status', teleop_status_qos
        )

        # How long a TeleopStatus is valid for (matches publish period
        # comfortably).  Consumers should treat locks as released once this
        # elapses without a refresh.
        self._teleop_status_timeout_sec = 0.5

        # ── State ──
        self._last_joy: Joy | None = None
        self._last_joy_time = None
        self._drive_engaged: bool = False     # LB held
        self._camera_engaged: bool = False    # RB held
        self._estop_latched: bool = False
        self._prev_center: bool = False
        self._pan: float = 0.0
        self._tilt: float = 0.0
        self._published_status = JoyTeleopStatus()  # last published, for diff

        # ── I/O ──
        self.create_subscription(Joy, '/joy', self._on_joy, 10)
        self._timer = None
        self._restart_timer()

        # ── Services ──
        self.create_service(GetJoyMapping, '~/get_mapping', self._srv_get)
        self.create_service(SetJoyMapping, '~/set_mapping', self._srv_set)

        # First status emit so subscribers see "no locks, enabled" immediately.
        self._publish_status(force=True)

        self.get_logger().info(
            f"Joy teleop ready (source={self._mapping_source}, "
            f"enabled={self._enabled}, "
            f"deadman=button {self._mapping['deadman_button']}, "
            f"camera_assist=button {self._mapping['camera_assist_button']}, "
            f"estop=button {self._mapping['estop_button']}, "
            f"max_speed={self._mapping['max_speed']} m/s, "
            f"max_steering={self._mapping['max_steering']}°)"
        )

    # ── Timer (rate is configurable, so it must be re-creatable) ──

    def _restart_timer(self) -> None:
        if self._timer is not None:
            self.destroy_timer(self._timer)
        period = 1.0 / max(1.0, float(self._mapping['rate']))
        self._timer = self.create_timer(period, self._tick)

    # ── Persistence ──

    def _load_mapping_from_file(self) -> None:
        """Load mapping atomically: any field rejection ignores the whole file.

        Partial-failure-silent loading hides corruption (one bad field leaves
        the rest applied with no visible error). Instead, validate everything
        first, then either commit the whole batch or log + use defaults.
        Bad file stays on disk — next valid save overwrites it.
        """
        if not os.path.isfile(MAPPING_FILE):
            return
        try:
            with open(MAPPING_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            self.get_logger().error(
                f'{MAPPING_FILE} cannot be read/parsed ({exc}); using defaults')
            return
        if not isinstance(data, dict):
            self.get_logger().error(
                f'{MAPPING_FILE} top-level is not a JSON object; using defaults')
            return

        # Optional metadata: "units": "degrees" or "radians".  Absent =>
        # legacy file (radians) and migration will fire.
        units = data.get('units')
        if isinstance(units, str) and units in ('degrees', 'radians'):
            self._mapping_units = units
        else:
            self._mapping_units = 'radians'  # treat unmarked as legacy

        validated: dict = {}
        for k, v in data.items():
            if k not in _ALL_KEYS:
                continue  # unknown keys silently ignored (forward compat)
            try:
                validated[k] = self._coerce(k, v)
            except (TypeError, ValueError) as exc:
                self.get_logger().error(
                    f'{MAPPING_FILE} invalid {k}={v!r} ({exc}); using defaults')
                return

        self._mapping.update(validated)
        self._mapping_source = 'file'
        self.get_logger().info(f'Loaded mapping from {MAPPING_FILE}')

    def _migrate_legacy_radians(self) -> None:
        """One-shot migration from the old radian-based limits to degrees.

        Pre-2026 builds stored max_steering/max_pan/max_tilt in radians
        (e.g. 0.45, 1.0, 0.6).  Newer files include a top-level
        ``"units": "degrees"`` marker so we can skip migration on every
        boot — without that marker we treat the file as legacy and
        upscale rad→deg, then rewrite the file with the marker.

        Note: we cannot safely use a numeric threshold (e.g. ``< 2.0``)
        as the trigger since the new UI allows ``max_steering`` as low
        as 1° and that would loop forever.
        """
        if self._mapping_units == 'degrees':
            return
        # Legacy file (or fresh defaults from yaml that already happen to
        # be in degrees).  Detect "looks-like-rad" by checking
        # max_steering — the only one with an unambiguous range:
        # legacy values were ≤ 1.5 rad, degree values are ≥ 1° but a
        # real user-facing min is 5°+.  Use 2.0 as the cutoff for
        # max_steering specifically.
        try:
            steer = float(self._mapping.get('max_steering', 0.0))
        except (TypeError, ValueError):
            steer = 0.0
        if steer >= 2.0:
            # Already degrees — just stamp the marker so we skip next boot.
            self._mapping_units = 'degrees'
            self._save_mapping_to_file()
            return
        # Legacy radians — convert all three.
        for key in ('max_steering', 'max_pan', 'max_tilt'):
            try:
                v = float(self._mapping.get(key, 0.0))
            except (TypeError, ValueError):
                continue
            if v > 0.0:
                self._mapping[key] = round(math.degrees(v), 1)
        self._mapping_units = 'degrees'
        self.get_logger().info(
            'Migrated legacy radian limits to degrees: '
            f"max_steering={self._mapping['max_steering']}°, "
            f"max_pan={self._mapping['max_pan']}°, "
            f"max_tilt={self._mapping['max_tilt']}°"
        )
        ok, msg = self._save_mapping_to_file()
        if not ok:
            self.get_logger().warning(f'Could not persist migrated mapping: {msg}')

    def _save_mapping_to_file(self) -> tuple[bool, str]:
        try:
            os.makedirs(os.path.dirname(MAPPING_FILE), exist_ok=True)
            tmp = MAPPING_FILE + '.tmp'
            payload = dict(self._mapping)
            payload['units'] = self._mapping_units
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(payload, f, indent=2, sort_keys=True)
                f.write('\n')
            os.replace(tmp, MAPPING_FILE)
            self._mapping_source = 'file'
            return True, f'Saved to {MAPPING_FILE}'
        except Exception as exc:  # noqa: BLE001
            return False, f'Save failed: {exc}'

    # ── Validation ──

    @staticmethod
    def _coerce(key: str, value: Any) -> Any:
        if key in _INT_KEYS:
            iv = int(value)
            if iv < -1:  # -1 disables that mapping
                raise ValueError('must be >= -1')
            return iv
        if key in _FLOAT_KEYS:
            fv = float(value)
            if fv < 0.0:
                raise ValueError('must be >= 0')
            return fv
        if key in _BOOL_KEYS:
            return bool(value)
        raise KeyError(f'unknown mapping key: {key}')

    # ── Parameter callback (handles `enabled` toggle) ──

    def _on_param_set(self, params):
        for p in params:
            if p.name == 'enabled' and p.type_ == Parameter.Type.BOOL:
                if self._enabled and not p.value:
                    # Disable: emit one zero frame so the car stops if we
                    # were actively driving, then stop publishing entirely.
                    self.pub_speed.publish(Float64(data=0.0))
                    self.pub_steer.publish(Float64(data=0.0))
                self._enabled = bool(p.value)
                self.get_logger().info(f'enabled = {self._enabled}')
                self._publish_status(force=True)
        return SetParametersResult(successful=True)

    # ── Service handlers ──

    def _srv_get(self, _req, resp):
        resp.success = True
        resp.message = ''
        resp.source = self._mapping_source
        resp.mapping_json = json.dumps(self._mapping, sort_keys=True)
        return resp

    def _srv_set(self, req, resp):
        try:
            updates: dict[str, Any] = {}
            if req.mapping_json:
                bulk = json.loads(req.mapping_json)
                if not isinstance(bulk, dict):
                    raise ValueError('mapping_json must be a JSON object')
                for k, v in bulk.items():
                    if k not in _ALL_KEYS:
                        raise ValueError(f'unknown key: {k}')
                    updates[k] = self._coerce(k, v)
            elif req.key:
                k = req.key
                if k not in _ALL_KEYS:
                    raise ValueError(f'unknown key: {k}')
                if k in _INT_KEYS:
                    updates[k] = self._coerce(k, req.int_value)
                elif k in _FLOAT_KEYS:
                    updates[k] = self._coerce(k, req.float_value)
                else:
                    updates[k] = self._coerce(k, req.bool_value)
            else:
                raise ValueError('either key or mapping_json must be set')

            # Apply atomically
            self._mapping.update(updates)
            if 'rate' in updates:
                self._restart_timer()

            msg = f'Updated {len(updates)} key(s)'
            if req.save_to_file:
                ok, save_msg = self._save_mapping_to_file()
                if not ok:
                    resp.success = False
                    resp.message = save_msg
                    resp.mapping_json = json.dumps(self._mapping, sort_keys=True)
                    return resp
                msg += f'; {save_msg}'
            else:
                # Runtime-only edit — mark source as live override
                if self._mapping_source != 'file':
                    self._mapping_source = 'override'

            resp.success = True
            resp.message = msg
            resp.mapping_json = json.dumps(self._mapping, sort_keys=True)
            return resp
        except Exception as exc:  # noqa: BLE001
            resp.success = False
            resp.message = str(exc)
            resp.mapping_json = json.dumps(self._mapping, sort_keys=True)
            return resp

    # ── Helpers ──

    def _axis(self, joy: Joy, idx: int, invert: bool) -> float:
        if idx < 0 or idx >= len(joy.axes):
            return 0.0
        v = float(joy.axes[idx])
        dz = float(self._mapping['deadzone'])
        if -dz < v < dz:
            v = 0.0
        if invert:
            v = -v
        return v

    def _btn(self, joy: Joy, idx: int) -> bool:
        if idx < 0 or idx >= len(joy.buttons):
            return False
        return bool(joy.buttons[idx])

    def _publish_status(self, force: bool = False) -> None:
        # Joy-specific status — only on change, since these fields rarely move.
        joy_msg = JoyTeleopStatus()
        joy_msg.enabled = self._enabled
        prev = self._published_status
        if force or joy_msg.enabled != prev.enabled:
            self.pub_status.publish(joy_msg)
            self._published_status = joy_msg

        # Generic teleop status — published *every* call (i.e. every tick).
        # Consumers use the carried timeout to detect a stale/dead publisher.
        teleop_msg = TeleopStatus()
        teleop_msg.source = 'joy'
        teleop_msg.drive_engaged = self._enabled and self._drive_engaged
        teleop_msg.camera_engaged = self._enabled and self._camera_engaged
        teleop_msg.estop_latched = self._estop_latched
        timeout_ns = int(self._teleop_status_timeout_sec * 1e9)
        teleop_msg.timeout = DurationMsg(
            sec=timeout_ns // 1_000_000_000,
            nanosec=timeout_ns % 1_000_000_000,
        )
        self.pub_teleop_status.publish(teleop_msg)

    # ── Callbacks ──

    def _on_joy(self, msg: Joy) -> None:
        self._last_joy = msg
        self._last_joy_time = self.get_clock().now()

        # Hold-to-activate ESTOP: active only while button is held.
        estop_now = self._btn(msg, int(self._mapping['estop_button']))
        if estop_now != self._estop_latched:
            self._estop_latched = estop_now
            state = 'ON' if self._estop_latched else 'OFF'
            self.get_logger().warning(f'Teleop ESTOP {state}')

        # Edge-trigger camera recenter (only meaningful when camera_engaged is
        # held — otherwise we'd fight another camera publisher).
        center_now = self._btn(msg, int(self._mapping['center_camera_button']))
        if center_now and not self._prev_center:
            self._pan = 0.0
            self._tilt = 0.0
        self._prev_center = center_now

    def _tick(self) -> None:
        joy = self._last_joy
        if joy is None or not self._enabled:
            # Even when disabled we still want status to reflect reality.
            self._drive_engaged = False
            self._camera_engaged = False
            self._publish_status()
            return

        # Stale-joy guard: ros-jazzy-joy publishes ~50Hz with autorepeat, so a
        # gap >0.5s means the controller dropped (BT disconnect, USB unplug,
        # node crash).  Treat that as "everything released" so locks fall and
        # other publishers (deepracer, REST) regain control instead of being
        # held off by a frozen last frame.
        if self._last_joy_time is not None:
            now = self.get_clock().now()
            age_ns = (now - self._last_joy_time).nanoseconds
            if age_ns > 500_000_000:
                if self._drive_engaged or self._camera_engaged:
                    self.get_logger().warning(
                        '/joy stale > 0.5s — releasing drive/camera engagement'
                    )
                self._drive_engaged = False
                self._camera_engaged = False
                self._publish_status()
                return

        prev_drive = self._drive_engaged
        prev_camera = self._camera_engaged

        self._drive_engaged = self._btn(joy, int(self._mapping['deadman_button']))
        self._camera_engaged = self._btn(joy, int(self._mapping['camera_assist_button']))

        # ── Drive (only while LB held and ESTOP not latched) ──
        if self._drive_engaged and not self._estop_latched:
            spd = self._axis(joy, int(self._mapping['axis_speed']), bool(self._mapping['invert_speed']))
            stg = self._axis(joy, int(self._mapping['axis_steering']), bool(self._mapping['invert_steering']))
            speed = _clip(spd * float(self._mapping['max_speed']), float(self._mapping['max_speed']))
            # max_steering is in degrees; /teleop/steering is radians.
            max_steer_deg = float(self._mapping['max_steering'])
            steering_deg = _clip(stg * max_steer_deg, max_steer_deg)
            self.pub_speed.publish(Float64(data=speed))
            self.pub_steer.publish(Float64(data=math.radians(steering_deg)))
        elif prev_drive and not self._drive_engaged:
            # Released — emit one zero so we don't leave the car coasting.
            self.pub_speed.publish(Float64(data=0.0))
            self.pub_steer.publish(Float64(data=0.0))
        elif self._estop_latched and self._drive_engaged:
            # ESTOP active while still gripping LB: keep zeroing speed.
            self.pub_speed.publish(Float64(data=0.0))

        # ── Camera (only while RB held; absolute position from stick) ──
        # Position mode (matches REST /control/camera/{pan,tilt}): full stick
        # deflection = max_pan/max_tilt, centre = 0°.  This makes joy
        # camera control as snappy as the web UI sliders — releasing the
        # stick instantly returns to centre, full tilt instantly drives
        # to the limit.  Internal _pan/_tilt are kept in DEGREES; we
        # convert to radians once at publish time.
        if self._camera_engaged:
            pan_in = self._axis(joy, int(self._mapping['axis_pan']), bool(self._mapping['invert_pan']))
            tilt_in = self._axis(joy, int(self._mapping['axis_tilt']), bool(self._mapping['invert_tilt']))
            self._pan = _clip(pan_in * float(self._mapping['max_pan']), float(self._mapping['max_pan']))
            self._tilt = _clip(tilt_in * float(self._mapping['max_tilt']), float(self._mapping['max_tilt']))
            # Always publish while RB is held so we keep ownership of the
            # lock period even when the stick is centred.
            self.pub_pan.publish(Float64(data=math.radians(self._pan)))
            self.pub_tilt.publish(Float64(data=math.radians(self._tilt)))
        elif prev_camera and not self._camera_engaged:
            # Released — publish current angles once so the camera holds the
            # final position rather than drifting back to whatever the
            # previous owner had cached.
            self.pub_pan.publish(Float64(data=math.radians(self._pan)))
            self.pub_tilt.publish(Float64(data=math.radians(self._tilt)))

        self._publish_status()


def main(args=None) -> None:
    rclpy.init(args=args)
    node = JoyTeleopNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
