#!/usr/bin/env python3
"""
Health check router for PhysiCar API.
"""

from datetime import datetime
from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
async def health_check():
    """
    Health check endpoint.
    
    Returns basic service status information.
    """
    return {
        "status": "ok",
        "service": "physicar-api",
        "timestamp": datetime.now().isoformat(),
    }
