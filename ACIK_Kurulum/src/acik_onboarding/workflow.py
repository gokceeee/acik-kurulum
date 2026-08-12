"""Durable workflow primitives for the onboarding process.

The Windows onboarding flow crosses reboots and user sessions.  A plain list
of booleans is not enough to describe that lifecycle, so this module keeps the
state contract small, versioned, and independently testable.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


STATE_SCHEMA_VERSION = 2

TASK_PENDING = "pending"
TASK_RUNNING = "running"
TASK_SUCCEEDED = "succeeded"
TASK_RETRYABLE_FAILED = "retryable_failed"
TASK_PERMANENT_FAILED = "permanent_failed"
TASK_SKIPPED = "skipped"

TERMINAL_TASK_STATUSES = {
    TASK_SUCCEEDED,
    TASK_PERMANENT_FAILED,
    TASK_SKIPPED,
}

USER_PHASE_TASKS = (
    "wifi_ready",
    "main_file_server",
    "network_printer",
    "desktop_wallpaper",
    "desktop_signature",
    "classic_outlook",
    "windows_update",
)

SYSTEM_PHASE_TASKS = (
    "eset",
    "lock_screen",
    "local_wallpaper_lock",
    "grant_ip_admin",
    "grant_administrator",
    "delete_x_user",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def make_task_map(enabled_tasks: Iterable[str]) -> dict[str, dict[str, object]]:
    enabled = set(enabled_tasks)
    return {
        task_name: {
            "enabled": task_name in enabled,
            "status": TASK_PENDING if task_name in enabled else TASK_SKIPPED,
            "attempts": 0,
            "error": "",
            "updated_at": utc_now(),
        }
        for task_name in (*USER_PHASE_TASKS, *SYSTEM_PHASE_TASKS)
    }


def phase_task_names(phase: str) -> tuple[str, ...]:
    if phase == "user":
        return USER_PHASE_TASKS
    if phase == "system":
        return SYSTEM_PHASE_TASKS
    raise ValueError(f"Bilinmeyen is akisi fazi: {phase}")


def enabled_phase_tasks(state: dict[str, object], phase: str) -> list[str]:
    tasks = state.get("tasks", {})
    if not isinstance(tasks, dict):
        return []
    selected: list[str] = []
    for name in phase_task_names(phase):
        item = tasks.get(name, {})
        if isinstance(item, dict) and bool(item.get("enabled")):
            selected.append(name)
    return selected


def unfinished_phase_tasks(state: dict[str, object], phase: str) -> list[str]:
    tasks = state.get("tasks", {})
    if not isinstance(tasks, dict):
        return []
    selected: list[str] = []
    for name in enabled_phase_tasks(state, phase):
        item = tasks.get(name, {})
        status = str(item.get("status", "")) if isinstance(item, dict) else ""
        if status not in TERMINAL_TASK_STATUSES:
            selected.append(name)
    return selected


def mark_task(
    state: dict[str, object],
    task_name: str,
    status: str,
    error: str = "",
) -> None:
    tasks = state.setdefault("tasks", {})
    if not isinstance(tasks, dict):
        raise ValueError("Is akisi tasks alani gecersiz.")
    task = tasks.setdefault(task_name, {})
    if not isinstance(task, dict):
        raise ValueError(f"Is akisi gorevi gecersiz: {task_name}")
    if status == TASK_RUNNING:
        task["attempts"] = int(task.get("attempts", 0)) + 1
    task["status"] = status
    task["error"] = error
    task["updated_at"] = utc_now()
    state["updated_at"] = utc_now()


def phase_status(state: dict[str, object], phase: str) -> str:
    names = enabled_phase_tasks(state, phase)
    if not names:
        return "not_required"
    tasks = state.get("tasks", {})
    statuses = {
        str(tasks[name].get("status", ""))
        for name in names
        if isinstance(tasks, dict) and isinstance(tasks.get(name), dict)
    }
    if statuses <= {TASK_SUCCEEDED, TASK_SKIPPED}:
        return "completed"
    if TASK_RUNNING in statuses:
        return "running"
    # A phase is only terminal when every enabled task is terminal. A permanent
    # failure must not hide another pending/retryable task and let the next
    # privileged phase start too early.
    if statuses <= TERMINAL_TASK_STATUSES and TASK_PERMANENT_FAILED in statuses:
        return "partial"
    return "pending"


def workflow_status(state: dict[str, object]) -> str:
    user_status = phase_status(state, "user")
    system_status = phase_status(state, "system")
    statuses = {user_status, system_status}
    if statuses <= {"completed", "not_required"}:
        return "completed"
    if "partial" in statuses:
        return "partial"
    if "running" in statuses:
        return "running"
    return "pending"


def validate_state(state: dict[str, object]) -> None:
    if int(state.get("schema_version", 0)) != STATE_SCHEMA_VERSION:
        raise ValueError("Bekleyen kurulum durumu bu uygulama surumuyle uyumlu degil.")
    if not str(state.get("run_id", "")).strip():
        raise ValueError("Bekleyen kurulum durumunda run_id eksik.")
    if not str(state.get("target_username", "")).strip():
        raise ValueError("Bekleyen kurulum durumunda hedef kullanici eksik.")
    tasks = state.get("tasks")
    if not isinstance(tasks, dict):
        raise ValueError("Bekleyen kurulum durumunda gorev listesi eksik.")


def atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def read_json(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON kok nesnesi gecersiz: {path}")
    return payload
