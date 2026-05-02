"""PhysiCar Agent - ROS2-style Tool API for LLM agents"""

from .core import (
    topic,      # topic['/odom'], topic.get(), topic.pub(), topic.list()
    service,    # service(name, req), service.list()
    action,     # action(name, goal), action.list()
    node,       # Direct ROS2 node access
    text,
    image,
)

__all__ = [
    'topic',
    'service',
    'action',
    'node',
    'text',
    'image',
]
