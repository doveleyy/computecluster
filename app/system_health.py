"""Pi host metrics and reachability of the services the dashboard reports on."""

import os
import platform
import socket
import sqlite3
import time
from typing import Any

import psutil

from app.app_health import collect_application_health
from app.service import JobService
from app.version import VERSION


def collect_system_metrics() -> dict[str, Any]:
    memory = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    load = os.getloadavg()
    return {
        "hostname": socket.gethostname(),
        "platform": platform.system(),
        "cpu": {
            "percent": psutil.cpu_percent(interval=None),
            "logical_cores": psutil.cpu_count(logical=True),
            "load_1": load[0],
            "load_5": load[1],
            "load_15": load[2],
            "temperature_c": cpu_temperature(),
        },
        "memory": {
            "total": memory.total,
            "used": memory.used,
            "available": memory.available,
            "percent": memory.percent,
        },
        "storage": {
            "mount": "/",
            "total": disk.total,
            "used": disk.used,
            "free": disk.free,
            "percent": disk.percent,
        },
        "uptime_seconds": uptime_seconds(),
    }


def collect_service_health(job_service: JobService) -> dict[str, Any]:
    try:
        job_service.ping()
        database_state = "ONLINE"
        database_detail = "SQLite ready"
    except sqlite3.Error:
        database_state = "OFFLINE"
        database_detail = "SQLite unavailable"

    synology_host = os.environ.get("HOME_PLATFORM_SYNOLOGY_HOST", "").strip()
    if synology_host:
        synology_smb_reachable = tcp_reachable(synology_host, 445)
        synology_dsm_reachable = tcp_reachable(synology_host, 5001)
        synology_state = "ONLINE" if synology_smb_reachable else "OFFLINE"
    else:
        synology_smb_reachable = False
        synology_dsm_reachable = False
        synology_state = "UNKNOWN"

    return {
        "control_plane": {
            "state": "ONLINE",
            "version": VERSION,
            "detail": "FastAPI coordination service",
        },
        "database": {
            "state": database_state,
            "engine": "SQLite",
            "detail": database_detail,
        },
        "synology_nas": {
            "state": synology_state,
            "configured": bool(synology_host),
            "host": synology_host or None,
            "smb": "reachable" if synology_smb_reachable else "unreachable",
            "management": ("reachable" if synology_dsm_reachable else "unreachable"),
            "role": "primary storage",
        },
        "applications": collect_application_health(),
    }


def tcp_reachable(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


def uptime_seconds() -> int:
    try:
        return max(0, int(time.time() - psutil.boot_time()))
    except OSError:
        return 0


def cpu_temperature() -> float | None:
    sensor_reader = getattr(psutil, "sensors_temperatures", None)
    if sensor_reader is None:
        return None
    try:
        temperatures = sensor_reader()
    except OSError:
        return None
    for readings in temperatures.values():
        if readings:
            return round(float(readings[0].current), 1)
    return None
