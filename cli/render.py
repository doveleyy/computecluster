"""Human-readable rendering for CLI output.

The API speaks JSON. People do not. Every command renders a compact view by
default and keeps the raw payload available behind --json, so the tool is
readable at a glance without becoming unscriptable.
"""

import re
from datetime import UTC, datetime
from typing import Any

BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
RESET = "\033[0m"

STATUS_COLOUR = {
    "COMPLETED": GREEN,
    "FAILED": RED,
    "RUNNING": YELLOW,
    "QUEUED": DIM,
}


def _age(timestamp: str | None) -> str:
    """Render an ISO timestamp as an approximate age, e.g. '3m' or '2d'."""
    if not timestamp:
        return "-"
    try:
        moment = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return "-"
    seconds = (datetime.now(UTC) - moment).total_seconds()
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def _percent(value: Any) -> str:
    return f"{value:.0f}%" if isinstance(value, int | float) else "-"


ANSI = re.compile(r"\033\[[0-9;]*m")


def _visible(text: str) -> int:
    """Printable width, ignoring colour escapes.

    len() counts escape sequences, so padding a coloured cell with ljust()
    misaligns the column by however many bytes the colour codes took.
    """
    return len(ANSI.sub("", text))


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _visible(text))


def _table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return f"{DIM}  (none){RESET}"
    widths = [
        max(_visible(headers[i]), *(_visible(row[i]) for row in rows))
        for i in range(len(headers))
    ]
    lines = [
        "  "
        + BOLD
        + "  ".join(
            _pad(header, widths[i]) for i, header in enumerate(headers)
        ).rstrip()
        + RESET
    ]
    for row in rows:
        lines.append(
            "  "
            + "  ".join(_pad(cell, widths[i]) for i, cell in enumerate(row)).rstrip()
        )
    return "\n".join(lines)


def workers(payload: Any) -> str:
    rows = []
    for worker in payload:
        metrics = worker.get("metrics") or {}
        scheduling = (
            f"{GREEN}ENABLED{RESET}" if worker["enabled"] else f"{DIM}disabled{RESET}"
        )
        rows.append(
            [
                worker["id"],
                scheduling,
                worker["state"],
                _percent(metrics.get("cpu_percent")),
                _percent(metrics.get("memory_percent")),
                _age(worker.get("last_seen")),
                (
                    f"{worker['max_job_cpu']:g} CPU / {worker['max_job_memory_mb']} MiB"
                    if worker.get("max_job_cpu") is not None
                    and worker.get("max_job_memory_mb") is not None
                    else "not configured"
                ),
                ", ".join(worker["supported_types"]),
            ]
        )
    return _table(
        [
            "WORKER",
            "SCHEDULING",
            "STATE",
            "CPU",
            "MEM",
            "SEEN",
            "JOB CEILING",
            "ACCEPTS",
        ],
        rows,
    )


def jobs(payload: Any) -> str:
    rows = []
    for job in payload:
        colour = STATUS_COLOUR.get(job["status"], "")
        rows.append(
            [
                f"{colour}{job['status']}{RESET}",
                job["type"],
                (job.get("name") or "-")[:30],
                job["id"][:8],
                _age(job.get("created_at")),
                job.get("target_worker_id") or "any",
                job.get("worker_id") or "-",
            ]
        )
    table = _table(["STATUS", "TYPE", "NAME", "ID", "AGE", "TARGET", "WORKER"], rows)
    return f"{table}\n{DIM}  {len(payload)} job(s){RESET}"


def job(payload: Any) -> str:
    colour = STATUS_COLOUR.get(payload["status"], "")
    lines = [
        f"{BOLD}{payload['id']}{RESET}",
        f"  name     {payload.get('name') or '-'}",
        f"  type     {payload['type']}",
        f"  status   {colour}{payload['status']}{RESET}",
        f"  target   {payload.get('target_worker_id') or 'any available worker'}",
        f"  worker   {payload.get('worker_id') or '-'}",
        f"  created  {_age(payload.get('created_at'))} ago"
        f"   attempt {payload.get('attempt')}/{payload.get('max_attempts')}",
    ]
    if payload.get("error"):
        lines.append(
            f"  {RED}reason   {payload.get('failure_kind') or 'EXECUTION_ERROR'}{RESET}"
        )
        lines.append(f"  {RED}error    {payload['error']}{RESET}")

    result = payload.get("result") or {}
    if result:
        lines.append(f"\n  {BOLD}result{RESET}")
        for key, value in result.items():
            if key in {"stdout", "stderr"}:
                continue
            lines.append(f"    {key:16} {value}")
        for stream in ("stdout", "stderr"):
            text = (result.get(stream) or "").strip()
            if text:
                lines.append(f"\n  {BOLD}{stream}{RESET}")
                lines.extend(f"    {line}" for line in text.splitlines()[-20:])
    return "\n".join(lines)


def submitted(payload: Any) -> str:
    short = payload["id"][:8]
    if "tasks" in payload:
        return (
            f"{GREEN}Submitted group{RESET} {BOLD}{short}{RESET}  "
            f'"{payload.get("name") or "-"}"\n'
            f"  status  {payload['status']}\n"
            f"  tasks   {len(payload['tasks'])}\n"
            f"{DIM}  inspect: client --json list{RESET}"
        )
    return (
        f"{GREEN}Submitted{RESET} {BOLD}{short}{RESET}  "
        f'"{payload.get("name") or "-"}"\n'
        f"  type    {payload['type']}\n"
        f"  status  {payload['status']}\n"
        f"  target  {payload.get('target_worker_id') or 'any available worker'}\n"
        f"{DIM}  watch:  client get {payload['id']}{RESET}"
    )


def cancelled(payload: Any) -> str:
    if payload["status"] == "FAILED":
        state = f"{RED}CANCELLED{RESET}"
        detail = "stopped before a worker claimed it"
    else:
        state = f"{YELLOW}CANCELLING{RESET}"
        detail = "the worker will stop it after its next heartbeat"
    return f"{BOLD}{payload['id']}{RESET}\n  state    {state}\n  detail   {detail}"


def worker_updated(payload: Any) -> str:
    if payload.get("max_job_cpu") is not None:
        capacity = (
            f"  ceiling {payload['max_job_cpu']:g} CPU / "
            f"{payload['max_job_memory_mb']} MiB"
        )
    else:
        capacity = ""
    state = (
        f"{GREEN}ENABLED{RESET}  (may now claim jobs)"
        if payload["enabled"]
        else f"{DIM}disabled{RESET}  (drains gracefully; running work finishes)"
    )
    return f"{BOLD}{payload['id']}{RESET}  scheduling {state}{capacity}"


def status(payload: Any) -> str:
    value = payload.get("status", "")
    colour = GREEN if value in {"healthy", "ready"} else RED
    return f"  {colour}{value}{RESET}"


def artifacts(payload: Any) -> str:
    if not payload:
        return (
            f"{DIM}  no published files{RESET}\n"
            f"{DIM}  (a job only publishes artifacts if it wrote to "
            f"HOME_PLATFORM_OUTPUT_DIR){RESET}"
        )
    rows = [
        [
            item["filename"],
            f"{item['size_bytes'] / 1024:.1f} KiB",
            item.get("sha256", "")[:12],
        ]
        for item in payload
    ]
    return _table(["FILE", "SIZE", "SHA-256"], rows)


def deleted(payload: Any) -> str:
    files = payload.get("deleted") or []
    freed = payload.get("freed_bytes", 0)
    if not files:
        return f"{DIM}  nothing to delete{RESET}"
    listed = "\n".join(f"    {name}" for name in files)
    return (
        f"{GREEN}Deleted{RESET} {len(files)} file(s), freed {freed / 1024:.1f} KiB\n"
        f"{listed}\n"
        f"{DIM}  the job record is unchanged{RESET}"
    )
