"""PhysiCar Agent - ROS2-style Tool API for LLM agents"""

from .core import (
    # ROS2-style API
    topic,      # topic['/odom'], topic.get(), topic.pub(), topic.list()
    service,    # service(name, req), service.list()
    action,     # action(name, goal), action.list()
    node,       # Direct ROS2 node access

    # Response helpers
    text,
    image,

    # Channel management
    get_stop_event,

    # Backwards compatibility (deprecated)
    state,
    publish,
    call_service,
    call_action,
    services,
    actions,
    get_node,
    refresh_topics,
)

__all__ = [
    # ROS2-style (recommended)
    'topic',
    'service',
    'action',
    'node',
    'text',
    'image',
    'get_stop_event',

    # Backwards compatibility
    'state',
    'publish',
    'call_service',
    'call_action',
    'services',
    'actions',
    'get_node',
    'refresh_topics',
]
