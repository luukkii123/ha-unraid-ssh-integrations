"""Durable, serial update jobs; remote commands survive an HA reload.

No byte progress is available from the template updater / Compose CLI. A job
therefore exposes phases, never a guessed percentage. An uncertain remote
result blocks the queue until observation establishes a terminal result.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import shlex
import uuid
from typing import Any

from . import actions
from .const import UPDATE_TIMEOUT
from .model import container_key, container_updatable, find_stack, find_container_by_key

ACTIVE = {"queued", "preparing", "pulling", "updating", "recreating", "verifying"}
TERMINAL = {"completed", "failed"}
POLL_SECONDS = 3


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def job_path(job_id: str) -> str:
    # Never accept an arbitrary path from persisted storage.
    uuid.UUID(hex=job_id)
    return f"/run/unraid-ssh-update-jobs/{job_id}"


def launch_command(job: dict) -> str:
    """Atomic remote admission. Reissuing this command never runs a job twice.

    Status writes use rename. No user-supplied name becomes a path or shell
    syntax. The command/result output stays out of HA attributes and logs.
    """
    path = shlex.quote(job_path(job["id"]))
    phase = "pulling" if job["compose"] else "updating"
    command = job["command"]
    # Compose's fixed builder emits this exact delimiter once.
    if job["compose"]:
        pull, recreate = command.split(" && ", 1) if not job.get("split_command") else job["split_command"]
        body = f"{pull}; r=$?; if [ $r -eq 0 ]; then mark recreating; {recreate}; r=$?; fi"
    else:
        body = f"{command}; r=$?"
    script = (
        f"d={path}; mark() {{ printf '%s\\n' \"$1\" > \"$d/status.new\" && mv \"$d/status.new\" \"$d/status\"; }}; "
        f"mark {phase}; {body}; if [ $r -eq 0 ]; then mark verifying; else mark failed:$r; fi"
    )
    return (f"umask 077; mkdir -p /run/unraid-ssh-update-jobs && "
            f"if mkdir {path} 2>/dev/null; then printf 'preparing\\n' > {path}/status; "
            f"nohup sh -c {shlex.quote(script)} </dev/null >/dev/null 2>&1 & fi")


def status_command(job: dict) -> str:
    return f"cat {shlex.quote(job_path(job['id']))}/status"


class UpdateQueue:
    def __init__(self, hass, entry, fast, updates, store) -> None:
        self.hass, self.entry, self.fast, self.updates, self.store = hass, entry, fast, updates, store
        self.jobs: list[dict[str, Any]] = []
        self.listeners: set = set()
        self._lock = asyncio.Lock()
        self._task = None
        self._closed = False

    async def load(self) -> None:
        data = await self.store.async_load()
        self.jobs = data.get("jobs", []) if data else []
        # Validate remote identities before accepting stored commands.
        for job in self.jobs:
            job_path(job["id"])
        self.start()

    def listen(self, callback):
        self.listeners.add(callback)
        return lambda: self.listeners.discard(callback)

    def target(self, key):
        return next((j for j in reversed(self.jobs) if j["key"] == key), None)

    def summary(self, stack: str | None = None) -> dict:
        jobs = [j for j in self.jobs if stack is None or j["stack"] == stack]
        current = next((j for j in jobs if j["state"] in ACTIVE - {"queued"}), None)
        completed = sum(j["state"] == "completed" for j in jobs)
        failed = sum(j["state"] == "failed" for j in jobs)
        total = len(jobs)
        return {"active": any(j["state"] in ACTIVE for j in jobs),
                "current_target": current["name"] if current else None,
                "queued_count": sum(j["state"] == "queued" for j in jobs),
                "completed_count": completed, "failed_count": failed, "total_count": total,
                "progress_percent": (completed + failed) / total * 100 if total else None,
                "blocked": any(j.get("last_error") and j["state"] in ACTIVE for j in jobs)}

    def attributes(self, key) -> dict:
        job = self.target(key)
        if not job:
            return {"update_state": "idle", "progress_percent": None, "queue_position": None,
                    "queued_at": None, "started_at": None, "finished_at": None, "last_error": None,
                    "update_queue": self.summary()}
        queued = [j for j in self.jobs if j["state"] == "queued"]
        return {"update_state": job["state"], "progress_percent": None,
                "queue_position": next((i + 1 for i, j in enumerate(queued) if j is job), None),
                **{k: job.get(k) for k in ("queued_at", "started_at", "finished_at", "last_error")},
                "update_queue": self.summary(), "stack_update_queue": self.summary(job["stack"])}

    async def changed(self):
        await self.store.async_save({"jobs": self.jobs})
        for listener in tuple(self.listeners):
            listener()

    async def enqueue(self, containers) -> None:
        """Validate all targets, save the complete batch, then start the worker."""
        async with self._lock:
            accepted = []
            snapshot = self.fast.data
            running_batch = any(j["state"] in ACTIVE for j in self.jobs)
            for container in containers:
                key = container_key(snapshot, container)
                if any(j["key"] == key and (running_batch or j["state"] in ACTIVE) for j in self.jobs + accepted):
                    continue
                stack = find_stack(snapshot, container.project) if container.project else None
                if not container_updatable(container, stack):
                    raise actions.ActionError("Update nicht möglich: Compose-Dateien fehlen. Konfiguration prüfen.")
                command = actions.container_update_cmd(container, stack)
                compose = bool(stack and container.service)
                split_command = None
                if compose:
                    # Retain environment setup for both phases (two independent shells).
                    pull, recreate = command.rsplit(" && ", 1)
                    prefix = pull[:pull.rfind("docker compose")]
                    split_command = [pull, prefix + recreate]
                accepted.append({"id": uuid.uuid4().hex, "key": key, "name": container.name,
                                 "stack": container.project, "state": "queued", "queued_at": now(),
                                 "started_at": None, "finished_at": None, "last_error": None,
                                 "command": command, "compose": compose, "split_command": split_command})
            if not accepted:
                return
            previous = list(self.jobs)
            if not any(j["state"] in ACTIVE for j in self.jobs):
                self.jobs = []
            self.jobs.extend(accepted)
            try:
                await self.changed()
            except BaseException:
                self.jobs = previous
                raise
            self.start()

    def start(self):
        if self._closed or not any(j["state"] in ACTIVE for j in self.jobs):
            return
        if self._task is None or self._task.done():
            self._task = self.hass.async_create_background_task(self.run(), "Unraid update queue")

    async def close(self):
        self._closed = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def run(self):
        while not self._closed:
            job = next((j for j in self.jobs if j["state"] in ACTIVE), None)
            if job is None:
                return
            try:
                if job["state"] == "queued":
                    # Persist launch intent FIRST. Recovery may safely issue the
                    # same atomic remote mkdir command even after a lost reply.
                    job.update(state="preparing", started_at=now())
                    await self.changed()
                if job["state"] == "preparing":
                    await actions.run_action(self.fast.client, launch_command(job), 30)
                result = await self.fast.client.run(status_command(job), timeout=30)
                phase = result.stdout.strip()
                if result.exit_status or phase not in {"preparing", "pulling", "updating", "recreating", "verifying"} and not phase.startswith("failed:"):
                    raise actions.ActionError("Backendstatus fehlt. Auftrag bleibt aktiv; Unraid-Verbindung und Job prüfen.")
                if phase.startswith("failed:"):
                    await self.fast.async_request_refresh()
                    await self.updates.async_request_refresh()
                    job.update(state="failed", finished_at=now(),
                               last_error="Update fehlgeschlagen. Weitere Aufträge laufen weiter. Unraid prüfen und Update erneut starten.")
                elif phase == "verifying":
                    job["state"] = "verifying"
                    await self.changed()
                    await self.fast.async_request_refresh()
                    await self.updates.async_request_refresh()
                    if not self.fast.last_update_success or not self.updates.last_update_success or {"docker", "compose", "stacks"} & self.fast.data.failed:
                        raise actions.ActionError("Prüfung nicht erreichbar. Auftrag bleibt aktiv; Verbindung prüfen.")
                    container = find_container_by_key(self.fast.data, job["key"])
                    status = self.updates.data.images.get(container.name) if self.updates.data and container else None
                    if status is None or status.update_available is None:
                        raise actions.ActionError("Image-Prüfung noch unvollständig. Auftrag bleibt aktiv; Registry-Verbindung prüfen.")
                    job.update(state="completed" if not status.update_available else "failed", finished_at=now(),
                               last_error=None if not status.update_available else "Nach dem Update ist das Image noch veraltet. Unraid prüfen und erneut versuchen.")
                else:
                    job["state"] = phase
                    elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(job["started_at"])).total_seconds()
                    job["last_error"] = ("Update dauert länger als erwartet. Auftrag bleibt aktiv; Unraid-Job prüfen. Nachfolgende Ziele warten."
                                         if elapsed > UPDATE_TIMEOUT else None)
                await self.changed()
            except Exception as err:
                # Do not publish SSH output, private addresses or shell content.
                job["last_error"] = (str(err) if isinstance(err, actions.ActionError) and str(err).startswith(("Backendstatus", "Prüfung", "Image-Prüfung"))
                                     else "Verbindung oder Jobstatus unklar. Auftrag bleibt aktiv; Unraid-Verbindung prüfen. Nachfolgende Ziele warten.")
                await self.changed()
            if job["state"] not in TERMINAL:
                await asyncio.sleep(POLL_SECONDS)
