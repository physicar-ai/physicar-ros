# PhysiCar API Routers

from . import health
from . import auth
from . import info
from . import kiosk
from . import hw
from . import calibration

__all__ = [
    "health",
    "auth",
    "info",
    "kiosk",
    "hw",
    "calibration",
]