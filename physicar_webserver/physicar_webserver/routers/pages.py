#!/usr/bin/env python3
#
# Copyright 2026 AICASTLE Inc.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""
Pages router — standalone UI pages.

Each page is a self-contained document that loads only the JS/CSS it
needs. Served via ``_load_html`` so /static asset references get
cache-busting ``?v=<mtime>`` query strings.

    /app        - main UI (Control + service tabs + Simulator + Sensors)
"""

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from physicar_webserver.routers.kiosk import _load_html

router = APIRouter(tags=["Pages"])


@router.get("/app", response_class=HTMLResponse)
async def app_page():
    return _load_html("app.html")

