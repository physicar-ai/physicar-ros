"""
ROS2 infrastructure — dynamic topic discovery, automatic QoS detection

ROS2 CLI-style API:
- topic['/odom'], topic.get(), topic.pub(), topic.list()
- service(name, req), service.list()
- action(name, goal), action.list()
- spin(seconds)

Response helpers:
- text(content) → {"type": "text", "text": ...}
- image(data, mime) → {"type": "image", "mime": ..., "base64": ...}
"""

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy
import time
import threading
import base64
import json
from typing import Dict, Any, Optional, Callable

# Type imports
from geometry_msgs.msg import Twist
from std_msgs.msg import Float64
from physicar_interfaces.msg import Audio


# ============================================
# Response helper functions
# ============================================

def text(content) -> dict:
    """Create a text response object
    
    Args:
        content: string, dict, or JSON-serialisable object
    
    Returns:
        {"type": "text", "text": "..."}
    """
    if isinstance(content, str):
        return {"type": "text", "text": content}
    elif isinstance(content, dict):
        return {"type": "text", "text": json.dumps(content, ensure_ascii=False)}
    else:
        return {"type": "text", "text": str(content)}


def image(data, mime: str = "image/jpeg") -> dict:
    """Create an image response object
    
    Args:
        data: bytes, CompressedImage, PIL.Image, or base64 string
        mime: MIME type (default: image/jpeg)
    
    Returns:
        {"type": "image", "mime": "...", "base64": "..."}
    """
    # Already a base64 string
    if isinstance(data, str):
        return {"type": "image", "mime": mime, "base64": data}
    
    # bytes
    if isinstance(data, bytes):
        return {"type": "image", "mime": mime, "base64": base64.b64encode(data).decode()}
    
    # ROS2 CompressedImage
    if hasattr(data, 'data') and hasattr(data, 'format'):
        fmt = data.format.lower() if data.format else 'jpeg'
        mime = f"image/{fmt}"
        return {"type": "image", "mime": mime, "base64": base64.b64encode(bytes(data.data)).decode()}
    
    # PIL Image
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
    
    # numpy array
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


def _get_message_class(type_string: str):
    """Get a class from a message-type string
    
    Args:
        type_string: e.g. 'sensor_msgs/msg/Imu'
    
    Returns:
        message class, or None
    """
    try:
        parts = type_string.split('/')
        if len(parts) != 3:
            return None
        
        package, _, msg_name = parts
        module = __import__(f'{package}.msg', fromlist=[msg_name])
        return getattr(module, msg_name, None)
    except Exception:
        return None


def _get_service_class(type_string: str):
    """Get a class from a service-type string
    
    Args:
        type_string: e.g. 'std_srvs/srv/SetBool'
    
    Returns:
        service class, or None
    """
    try:
        parts = type_string.split('/')
        if len(parts) != 3:
            return None
        
        package, _, srv_name = parts
        module = __import__(f'{package}.srv', fromlist=[srv_name])
        return getattr(module, srv_name, None)
    except Exception:
        return None


def _get_action_class(type_string: str):
    """Get a class from an action-type string
    
    Args:
        type_string: e.g. 'nav2_msgs/action/NavigateToPose'
    
    Returns:
        action class, or None
    """
    try:
        parts = type_string.split('/')
        if len(parts) != 3:
            return None
        
        package, _, action_name = parts
        module = __import__(f'{package}.action', fromlist=[action_name])
        return getattr(module, action_name, None)
    except Exception:
        return None


def _msg_to_dict(msg) -> Any:
    """Convert a ROS2 message to a dict (including nested fields)"""
    if msg is None:
        return None
    
    # Primitive types
    if isinstance(msg, (bool, int, float, str)):
        return msg
    
    # List/tuple
    if isinstance(msg, (list, tuple)):
        return [_msg_to_dict(item) for item in msg]
    
    # bytes-like
    if isinstance(msg, (bytes, bytearray)):
        return bytes(msg)  # keep as-is (base64 conversion handled separately)
    
    # array.array (e.g. LaserScan.ranges)
    try:
        import array
        if isinstance(msg, array.array):
            return list(msg)
    except ImportError:
        pass
    
    # numpy array
    try:
        import numpy as np
        if isinstance(msg, np.ndarray):
            return msg.tolist()
    except ImportError:
        pass
    
    # ROS2 message → dict
    if hasattr(msg, 'get_fields_and_field_types'):
        result = {}
        for field_name in msg.get_fields_and_field_types().keys():
            value = getattr(msg, field_name, None)
            result[field_name] = _msg_to_dict(value)
        return result
    
    # Otherwise: convert to str
    return str(msg)


# Whitelist: only subscribe to topics that tools actually use.
# Based on physicar-ros README — sensor data, control, and state topics.
_ALLOWED_TOPICS = frozenset((
    # Sensor / state
    '/scan', '/scan_filtered',
    '/imu', '/imu/mag',
    '/odom', '/odom/laser',
    '/camera/image_raw/compressed', '/camera/camera_info',
    '/battery_state',
    '/joint_states',
    '/physicar_driver/calibration_status',
    '/deepracer/inference',
    # Control
    '/speed', '/steering',
    '/cmd_vel', '/cmd_vel_nav', '/cmd_vel_teleop', '/cmd_vel_smoothed',
    '/camera/pan', '/camera/tilt',
    '/audio',
    # Teleop
    '/teleop/speed', '/teleop/steering',
    '/teleop/camera/pan', '/teleop/camera/tilt',
    '/teleop/status',
    '/joy', '/joy/set_feedback',
    '/physicar_joy_teleop/status',
    '/preempt_teleop',
    # Navigation (high-level only)
    '/map', '/plan', '/goal_pose', '/initialpose',
    '/amcl_pose', '/particle_cloud',
    '/global_costmap/costmap', '/local_costmap/costmap',
    # Misc
    '/robot_description',
    '/servo/commands',
    # Agent internal
    '/agent/tool/reload',
    '/clock',
))

def _should_skip_topic(topic: str) -> bool:
    """Return True for topics NOT in the whitelist."""
    return topic not in _ALLOWED_TOPICS


class _DynamicState:
    """Dynamic topic-state container
    
    Access the latest message via state['/topic/name']
    Lazy dict conversion — raw msg stored on callback,
    dict computed only on first read and cached until next msg.
    """
    
    def __init__(self):
        self._raw: Dict[str, Any] = {}      # Raw original messages
        self._cache: Dict[str, Any] = {}    # Lazy-converted dicts
        self._dirty: Dict[str, bool] = {}   # True if raw updated since last cache
        self._lock = threading.Lock()
    
    def _update(self, topic: str, msg):
        """Update topic state (store raw only, mark dirty)"""
        with self._lock:
            self._raw[topic] = msg
            self._dirty[topic] = True
    
    def _get_dict(self, topic: str):
        """Return cached dict, converting lazily if dirty. Must hold lock."""
        if self._dirty.get(topic, False):
            raw = self._raw.get(topic)
            if raw is not None:
                self._cache[topic] = _msg_to_dict(raw)
            self._dirty[topic] = False
        return self._cache.get(topic)
    
    def __getitem__(self, topic: str) -> Any:
        """Read topic state (dict-converted value)"""
        with self._lock:
            return self._get_dict(topic)
    
    def get(self, topic: str, default=None) -> Any:
        """Read topic state (with default)"""
        with self._lock:
            if topic not in self._raw:
                return default
            val = self._get_dict(topic)
            return val if val is not None else default
    
    def get_raw(self, topic: str):
        """Read raw ROS2 message"""
        with self._lock:
            return self._raw.get(topic)
    
    def keys(self):
        """List subscribed topics"""
        with self._lock:
            return list(self._raw.keys())
    
    def items(self):
        """(topic, state) pairs"""
        with self._lock:
            return [(t, self._get_dict(t)) for t in self._raw]
    
    def __contains__(self, topic: str) -> bool:
        with self._lock:
            return topic in self._raw
    
    def __repr__(self):
        with self._lock:
            topics = list(self._raw.keys())
        return f"<State topics={topics}>"


class _AgentCore:
    """Agent core singleton"""
    
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance
    
    def __init__(self, external_node=None):
        if self._initialized:
            return
        self._initialized = True
        
        # Use external node or create our own
        if external_node is not None:
            self._node = external_node
            self._owns_node = False
        else:
            # Initialise ROS2 if needed
            if not rclpy.ok():
                rclpy.init()
            self._node = rclpy.create_node('agent_core')
            self._owns_node = True
        
        self._state = _DynamicState()
        self._publishers: Dict[str, Any] = {}
        self._subscriptions: Dict[str, Any] = {}
        self._service_clients: Dict[str, Any] = {}  # {name: (client, srv_class)}
        self._action_clients: Dict[str, Any] = {}   # {name: (client, action_class)}
        self._spinning = False
        self._spin_thread = None
        
        # Default QoS (fallback)
        self._default_qos = QoSProfile(
            reliability=ReliabilityPolicy.RELIABLE,
            history=HistoryPolicy.KEEP_LAST,
            depth=10
        )
        
        # Dynamic discovery and registration
        self._discover_and_subscribe()  # topics
        self._discover_services()        # services
        self._discover_actions()         # actions
        
        # Periodic re-discovery: pick up topics/services that appear after
        # initial startup (e.g. rf2o /odom which needs /scan_filtered first).
        self._refresh_timer = self._node.create_timer(
            2.0, self._periodic_refresh
        )
        
        # Background spin removed — agent_node owns spinning
        # self._start_spin_thread()
    
    def _get_publisher_qos(self, topic: str):
        """Detect existing publisher QoS on the topic (safe path)"""
        try:
            pubs = self._node.get_publishers_info_by_topic(topic)
            if pubs:
                qos = pubs[0].qos_profile
                # Fall back to default QoS if any policy is UNKNOWN
                if qos.history == HistoryPolicy.UNKNOWN:
                    return self._default_qos
                if qos.reliability == ReliabilityPolicy.UNKNOWN:
                    return self._default_qos
                return qos
        except Exception:
            pass
        return self._default_qos
    
    def _get_subscriber_qos(self, topic: str):
        """Detect existing subscriber QoS on the topic (safe path)"""
        try:
            subs = self._node.get_subscriptions_info_by_topic(topic)
            if subs:
                qos = subs[0].qos_profile
                # Fall back to default QoS if any policy is UNKNOWN
                if qos.history == HistoryPolicy.UNKNOWN:
                    return self._default_qos
                if qos.reliability == ReliabilityPolicy.UNKNOWN:
                    return self._default_qos
                return qos
        except Exception:
            pass
        return self._default_qos
    
    def _discover_and_subscribe(self):
        """Discover live topics and subscribe (auto-detect QoS)"""
        # Brief wait to collect topic info
        for _ in range(10):
            rclpy.spin_once(self._node, timeout_sec=0.1)
        
        # Fetch topic list
        topic_list = self._node.get_topic_names_and_types()
        
        subscribed_count = 0
        skipped_count = 0
        for topic, types in topic_list:
            if not types:
                continue
            
            # Skip internal ROS2 topics
            if _should_skip_topic(topic):
                skipped_count += 1
                continue
            
            # Use the first declared type
            msg_type_str = types[0]
            msg_class = _get_message_class(msg_type_str)
            
            if msg_class is None:
                self._node.get_logger().debug(f"Unknown message type: {msg_type_str}")
                continue
            
            # Auto-detect QoS (matches publisher's QoS)
            qos = self._get_publisher_qos(topic)
            
            # Create subscription
            self._create_subscription(topic, msg_class, qos)
            subscribed_count += 1
        
        self._node.get_logger().info(f"Discovered and subscribed to {subscribed_count} topics (skipped {skipped_count} internal)")
        
        # Wait for initial messages
        for _ in range(20):
            rclpy.spin_once(self._node, timeout_sec=0.1)
    
    def _create_subscription(self, topic: str, msg_class, qos):
        """Create a topic subscription"""
        def callback(msg, topic=topic):
            self._state._update(topic, msg)
        
        sub = self._node.create_subscription(msg_class, topic, callback, qos)
        self._subscriptions[topic] = sub
    
    def _should_create_service_client(self, name: str) -> bool:
        """Whether to create a service client"""
        return True
    
    def _should_create_action_client(self, name: str) -> bool:
        """Whether to create an action client"""
        return True
    
    def _discover_services(self):
        """Discover services and create clients"""
        service_list = self._node.get_service_names_and_types()
        
        count = 0
        for name, types in service_list:
            if not self._should_create_service_client(name):
                continue
            
            if not types:
                continue
            
            srv_type_str = types[0]
            srv_class = _get_service_class(srv_type_str)
            
            if srv_class is None:
                continue
            
            client = self._node.create_client(srv_class, name)
            self._service_clients[name] = (client, srv_class)
            count += 1
        
        self._node.get_logger().info(f"Discovered {count} services")
    
    def _discover_actions(self):
        """Discover actions and create clients"""
        try:
            from rclpy.action import ActionClient
        except ImportError:
            self._node.get_logger().warn("Action support not available")
            return
        
        # Actions are discovered via the _action/status topic pattern
        topic_list = self._node.get_topic_names_and_types()
        action_names = set()
        
        for topic, _ in topic_list:
            if '/_action/status' in topic:
                # /namespace/action_name/_action/status -> /namespace/action_name
                action_name = topic.replace('/_action/status', '')
                action_names.add(action_name)
        
        count = 0
        for action_name in action_names:
            if not self._should_create_action_client(action_name):
                continue
            
            # Find the action type via the _action/feedback topic
            feedback_topic = f"{action_name}/_action/feedback"
            action_type_str = None
            
            for topic, types in topic_list:
                if topic == feedback_topic and types:
                    # 'pkg/action/Name_FeedbackMessage' -> 'pkg/action/Name'
                    full_type = types[0]
                    if '_FeedbackMessage' in full_type:
                        action_type_str = full_type.replace('_FeedbackMessage', '')
                    break
            
            if not action_type_str:
                continue
            
            action_class = _get_action_class(action_type_str)
            if action_class is None:
                continue
            
            client = ActionClient(self._node, action_class, action_name)
            self._action_clients[action_name] = (client, action_class)
            count += 1
        
        self._node.get_logger().info(f"Discovered {count} actions")
    
    def _start_spin_thread(self):
        """Start the background spin thread"""
        if self._spinning:
            return
        
        self._spinning = True
        self._spin_thread = threading.Thread(target=self._spin_loop, daemon=True)
        self._spin_thread.start()
        self._node.get_logger().info("Background spin thread started")
    
    def _spin_loop(self):
        """Background spin loop — receives real-time messages"""
        while self._spinning and rclpy.ok():
            try:
                rclpy.spin_once(self._node, timeout_sec=0.01)
            except Exception:
                pass
    
    def shutdown(self):
        """Cleanup"""
        self._spinning = False
        if self._spin_thread and self._spin_thread.is_alive():
            self._spin_thread.join(timeout=1.0)
        # Only destroy nodes we created ourselves
        if self._owns_node and rclpy.ok():
            self._node.destroy_node()
    
    def _periodic_refresh(self):
        """Timer callback — discover newly appeared topics, services, and actions."""
        self.refresh_topics()
        self._refresh_services()
        self._refresh_actions()

    def refresh_topics(self):
        """Refresh topic list (discover new topics, auto-detect QoS)"""
        topic_list = self._node.get_topic_names_and_types()
        
        new_count = 0
        for topic, types in topic_list:
            # Skip if already subscribed
            if topic in self._subscriptions:
                continue
            
            if not types:
                continue
            
            # Skip internal ROS2 topics
            if _should_skip_topic(topic):
                continue
            
            msg_type_str = types[0]
            msg_class = _get_message_class(msg_type_str)
            
            if msg_class is None:
                continue
            
            # Auto-detect QoS
            qos = self._get_publisher_qos(topic)
            self._create_subscription(topic, msg_class, qos)
            new_count += 1
        
        if new_count > 0:
            self._node.get_logger().info(f"Subscribed to {new_count} new topics")
        
        return new_count
    
    def _refresh_services(self):
        """Discover newly appeared services and create clients."""
        service_list = self._node.get_service_names_and_types()
        new_count = 0
        for name, types in service_list:
            if name in self._service_clients:
                continue
            if not self._should_create_service_client(name):
                continue
            if not types:
                continue
            srv_class = _get_service_class(types[0])
            if srv_class is None:
                continue
            client = self._node.create_client(srv_class, name)
            self._service_clients[name] = (client, srv_class)
            new_count += 1
        if new_count > 0:
            self._node.get_logger().info(f"Registered {new_count} new service clients")
    
    def _refresh_actions(self):
        """Discover newly appeared actions and create clients."""
        try:
            from rclpy.action import ActionClient
        except ImportError:
            return
        topic_list = self._node.get_topic_names_and_types()
        action_names = set()
        for t, _ in topic_list:
            if '/_action/status' in t:
                action_names.add(t.replace('/_action/status', ''))
        new_count = 0
        for action_name in action_names:
            if action_name in self._action_clients:
                continue
            if not self._should_create_action_client(action_name):
                continue
            feedback_topic = f"{action_name}/_action/feedback"
            action_type_str = None
            for t, types in topic_list:
                if t == feedback_topic and types:
                    full_type = types[0]
                    if '_FeedbackMessage' in full_type:
                        action_type_str = full_type.replace('_FeedbackMessage', '')
                    break
            if not action_type_str:
                continue
            action_class = _get_action_class(action_type_str)
            if action_class is None:
                continue
            client = ActionClient(self._node, action_class, action_name)
            self._action_clients[action_name] = (client, action_class)
            new_count += 1
        if new_count > 0:
            self._node.get_logger().info(f"Registered {new_count} new action clients")
    
    @property
    def node(self):
        return self._node
    
    @property
    def state(self):
        return self._state
    
    def publish(self, topic: str, msg):
        """Publish a message on a topic (auto-detect QoS)"""
        if topic not in self._publishers:
            # Auto-detect QoS (match subscriber QoS)
            qos = self._get_subscriber_qos(topic)
            
            # Create publisher dynamically
            msg_type = type(msg)
            self._publishers[topic] = self._node.create_publisher(msg_type, topic, qos)
            
            # Wait for connection (background spin handles it; just sleep)
            for _ in range(10):
                time.sleep(0.1)
                if self._publishers[topic].get_subscription_count() > 0:
                    break
        
        self._publishers[topic].publish(msg)
    
    def wait(self, seconds: float):
        """Wait (background spin handles callbacks)"""
        if seconds <= 0:
            time.sleep(0.01)
            return
        time.sleep(seconds)
    
    def call_service(self, name: str, request=None, timeout: float = 5.0) -> Any:
        """
        Call a service
        
        Args:
            name: service name (e.g. '/set_bool')
            request: Request object or dict
            timeout: timeout in seconds
        
        Returns:
            Response object converted to a dict
        """
        if name not in self._service_clients:
            raise ValueError(f"Service not found: {name}. Available: {list(self._service_clients.keys())}")
        
        client, srv_class = self._service_clients[name]
        
        # Convert dict → Request
        if request is None:
            req = srv_class.Request()
        elif isinstance(request, dict):
            req = srv_class.Request()
            for key, value in request.items():
                if hasattr(req, key):
                    setattr(req, key, value)
        else:
            req = request
        
        # Wait for service
        if not client.wait_for_service(timeout_sec=timeout):
            raise TimeoutError(f"Service {name} not available")
        
        # Call
        future = client.call_async(req)
        
        # Wait for response
        start = time.time()
        while not future.done() and (time.time() - start) < timeout:
            time.sleep(0.01)
        
        if not future.done():
            raise TimeoutError(f"Service {name} call timeout")
        
        return _msg_to_dict(future.result())
    
    def call_action(self, name: str, goal=None, timeout: float = 30.0, feedback_callback: Callable = None) -> Any:
        """
        Call an action (blocking)
        
        Args:
            name: action name (e.g. '/navigate_to_pose')
            goal: Goal object or dict
            timeout: timeout in seconds
            feedback_callback: optional feedback callback
        
        Returns:
            Result object converted to a dict
        """
        if name not in self._action_clients:
            raise ValueError(f"Action not found: {name}. Available: {list(self._action_clients.keys())}")
        
        client, action_class = self._action_clients[name]
        
        # Convert dict → Goal
        if goal is None:
            goal_msg = action_class.Goal()
        elif isinstance(goal, dict):
            goal_msg = action_class.Goal()
            for key, value in goal.items():
                if hasattr(goal_msg, key):
                    setattr(goal_msg, key, value)
        else:
            goal_msg = goal
        
        # Wait for server
        if not client.wait_for_server(timeout_sec=timeout):
            raise TimeoutError(f"Action server {name} not available")
        
        # Send goal
        send_goal_future = client.send_goal_async(goal_msg, feedback_callback=feedback_callback)
        
        # Wait for goal acceptance
        start = time.time()
        while not send_goal_future.done() and (time.time() - start) < timeout:
            time.sleep(0.01)
        
        if not send_goal_future.done():
            raise TimeoutError(f"Action {name} goal send timeout")
        
        goal_handle = send_goal_future.result()
        if not goal_handle.accepted:
            raise RuntimeError(f"Action {name} goal rejected")
        
        # Wait for result
        result_future = goal_handle.get_result_async()
        
        start = time.time()
        while not result_future.done() and (time.time() - start) < timeout:
            time.sleep(0.01)
        
        if not result_future.done():
            raise TimeoutError(f"Action {name} result timeout")
        
        return _msg_to_dict(result_future.result().result)
    
    def get_services(self) -> list:
        """List registered services"""
        return list(self._service_clients.keys())
    
    def get_actions(self) -> list:
        """List registered actions"""
        return list(self._action_clients.keys())
    
    def get_subscribed_topics(self) -> list:
        """List of (name, type) tuples for subscribed topics"""
        topic_list = self._node.get_topic_names_and_types()
        topic_map = {name: types[0] for name, types in topic_list if types}
        return [(name, topic_map.get(name, 'unknown')) for name in self._subscriptions]


# Singleton instance
_core = None


def _get_core():
    """Return the core instance (lazy init)"""
    global _core
    if _core is None:
        _core = _AgentCore()
    return _core


# ============================================
# Public API — ROS2 style
# ============================================

class _TopicProxy:
    """Topic proxy — ros2 topic style
    
    Usage:
        topic['/odom']           # Latest message (dict)
        topic.get('/odom', {})   # With default
        topic.raw('/odom')       # Raw ROS2 message
        topic.pub('/cmd_vel', msg)  # Publish
        topic.list()             # List subscribed topics
    """
    
    def __getitem__(self, name: str) -> Any:
        """Read topic data"""
        return _get_core().state[name]
    
    def get(self, name: str, default=None) -> Any:
        """Read topic data (with default)"""
        return _get_core().state.get(name, default)
    
    def raw(self, name: str):
        """Raw ROS2 message"""
        return _get_core().state.get_raw(name)
    
    def pub(self, name: str, msg):
        """Publish a message on a topic"""
        _get_core().publish(name, msg)
    
    def list(self) -> list:
        """List of (topic_name, type) tuples for subscribed topics"""
        return _get_core().get_subscribed_topics()
    
    def refresh(self) -> int:
        """Discover and subscribe to new topics"""
        return _get_core().refresh_topics()
    
    def __contains__(self, name: str) -> bool:
        return name in _get_core().state
    
    def __repr__(self):
        return f"<topic {_get_core().state.keys()}>"


class _ServiceProxy:
    """Service proxy — ros2 service style
    
    Usage:
        service('/set_mode', {'mode': 1})  # Call a service
        service.list()                      # List services
    """
    
    def __call__(self, name: str, request=None, timeout: float = 5.0) -> Any:
        """Call a service"""
        return _get_core().call_service(name, request, timeout)
    
    def list(self) -> list:
        """List registered services"""
        return _get_core().get_services()
    
    def __repr__(self):
        return f"<service {self.list()}>"


class _ActionProxy:
    """Action proxy — ros2 action style
    
    Usage:
        action('/navigate', {'target': ...})  # Call an action
        action.list()                          # List actions
    """
    
    def __call__(self, name: str, goal=None, timeout: float = 30.0, feedback_callback: Callable = None) -> Any:
        """Call an action"""
        return _get_core().call_action(name, goal, timeout, feedback_callback)
    
    def list(self) -> list:
        """List registered actions"""
        return _get_core().get_actions()
    
    def __repr__(self):
        return f"<action {self.list()}>"


# Proxy instances
topic = _TopicProxy()
service = _ServiceProxy()
action = _ActionProxy()


# Direct access to the ROS2 node (advanced)
class _NodeProperty:
    """Attribute-style access to the underlying node"""
    def __repr__(self):
        return repr(_get_core().node)
    
    def __getattr__(self, name):
        return getattr(_get_core().node, name)

node = _NodeProperty()


def shutdown():
    """Clean up the core (call before process exit)"""
    global _core
    if _core:
        _core.shutdown()
        _core = None


# Register atexit so cleanup runs on process exit
import atexit
atexit.register(shutdown)
