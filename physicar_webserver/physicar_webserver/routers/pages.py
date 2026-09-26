#!/usr/bin/env python3
#
# SPDX-License-Identifier: LicenseRef-PhysiCar-Community-1.0
# Copyright (c) 2026 AICASTLE Inc.
# Licensed under the PhysiCar Community License 1.0 (see LICENSE).

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

