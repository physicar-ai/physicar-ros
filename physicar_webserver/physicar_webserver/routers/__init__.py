# PhysiCar API Routers

from . import health
from . import auth
from . import info
from . import kiosk
from . import state
from . import control
from . import agent
from . import calibration
from . import deepracer

__all__ = [
    "health",
    "auth", 
    "info",
    "kiosk",
    "state",
    "control",
    "agent",
    "calibration",
    "deepracer",
]