"""The snapshot one poll produces, assembled section by section.

Each section is parsed on its own: a parser that raises marks its section in
`Snapshot.failed` and leaves the others intact. Stacks come from the
compose.manager project folders (so a downed stack keeps existing) and are
enriched with `docker compose ls` and the container labels.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Callable

from . import parse
from .parse import ArrayInfo, Container, CpuTimes, Disk, Gpu, Load, Memory, Share, Vm

_INVALID_PROJECT_CHARS = re.compile(r"[^a-z0-9_-]+")


def normalize_project_name(name: str) -> str:
    """Compose project-name rules: lowercase, [a-z0-9_-], no leading/trailing -/_.

    A run of invalid characters collapses into a single `-`, the way compose
    itself turns a folder like `Claude Station` into `claude-station`.
    """
    return _INVALID_PROJECT_CHARS.sub("-", name.strip().lower()).strip("-_")


@dataclass(frozen=True)
class Stack:
    name: str
    folder: str                # compose.manager project folder, "" when only seen in compose ls
    autostart: bool
    present: bool              # listed by `docker compose ls -a`
    running: int
    config_files: tuple[str, ...]
    containers: tuple[Container, ...]


def _match_projects(
    stack_dirs: list[parse.StackDir],
    projects: list[parse.ComposeProject],
) -> dict[int, parse.ComposeProject]:
    """Map folder index -> compose project.

    Two rules. First the names: the folder's `name` file, normalized, usually
    *is* the project name. Where it is not — `gps-bridge` on disk, `gps_bridge`
    in compose — the project's config files still lie inside the folder, and
    that path is what ties the two together. All name matches are resolved
    before any path match, so the order of the folders cannot let one folder
    take a project that another folder is actually named after.
    """
    matched: dict[int, parse.ComposeProject] = {}
    claimed: set[str] = set()
    for index, folder in enumerate(stack_dirs):
        normalized = normalize_project_name(folder.name)
        for project in projects:
            if project.name not in claimed and project.name == normalized:
                matched[index] = project
                claimed.add(project.name)
                break
    for index, folder in enumerate(stack_dirs):
        if index in matched:
            continue
        prefix = folder.path if folder.path.endswith("/") else folder.path + "/"
        for project in projects:
            if project.name in claimed:
                continue
            if any(f.startswith(prefix) for f in project.config_files):
                matched[index] = project
                claimed.add(project.name)
                break
    return matched


def merge_stacks(
    stack_dirs: list[parse.StackDir],
    projects: list[parse.ComposeProject],
    containers: list[Container],
) -> tuple[tuple[Stack, ...], tuple[Container, ...]]:
    by_project: dict[str, list[Container]] = {}
    loose: list[Container] = []
    for container in containers:
        if container.project:
            by_project.setdefault(container.project, []).append(container)
        else:
            loose.append(container)

    matched = _match_projects(stack_dirs, projects)
    stacks: list[Stack] = []
    for index, folder in enumerate(stack_dirs):
        info = matched.get(index)
        # The compose project name is what `docker ps` labels and what every
        # compose command takes, so a matched stack carries it; an unmatched
        # folder falls back to its own normalized name.
        name = info.name if info else normalize_project_name(folder.name)
        stacks.append(
            Stack(
                name=name,
                folder=folder.path,
                autostart=folder.autostart,
                present=info is not None,
                running=info.running if info else 0,
                config_files=info.config_files if info else (),
                containers=tuple(by_project.get(name, ())),
            )
        )
    taken = {info.name for info in matched.values()}
    for info in projects:
        if info.name in taken:
            continue
        taken.add(info.name)
        stacks.append(
            Stack(
                name=info.name, folder="", autostart=False, present=True,
                running=info.running, config_files=info.config_files,
                containers=tuple(by_project.get(info.name, ())),
            )
        )
    return tuple(stacks), tuple(loose)


@dataclass(frozen=True)
class Snapshot:
    array: ArrayInfo | None
    disks: tuple[Disk, ...]
    shares: tuple[Share, ...]
    cpu_times: CpuTimes | None
    cpu_percent: float | None
    memory: Memory | None
    load: Load | None
    gpus: tuple[Gpu, ...]
    stacks: tuple[Stack, ...]
    template_containers: tuple[Container, ...]
    containers: tuple[Container, ...]
    vms: tuple[Vm, ...]
    failed: frozenset[str]


def _section(sections: dict[str, str], name: str, fn: Callable[[str], Any], failed: set[str], default: Any) -> Any:
    text = sections.get(name)
    if text is None:
        failed.add(name)
        return default
    try:
        return fn(text)
    except Exception:  # noqa: BLE001 — one bad section must not sink the poll
        failed.add(name)
        return default


def build_snapshot(sections: dict[str, str], previous: Snapshot | None) -> Snapshot:
    failed: set[str] = set()
    array = _section(sections, "var", parse.parse_var, failed, None)
    disks = tuple(_section(sections, "disks", parse.parse_disks, failed, []))
    shares = tuple(_section(sections, "shares", parse.parse_shares, failed, []))
    cpu_times = _section(sections, "stat", parse.parse_proc_stat, failed, None)
    memory = _section(sections, "mem", parse.parse_meminfo, failed, None)
    load = _section(sections, "load", parse.parse_load, failed, None)
    gpus = tuple(_section(sections, "gpu", parse.parse_gpus, failed, []))
    containers = _section(sections, "docker", parse.parse_containers, failed, [])
    projects = _section(sections, "compose", parse.parse_compose_ls, failed, [])
    stack_dirs = _section(sections, "stacks", parse.parse_stack_dirs, failed, [])
    vms = tuple(_section(sections, "vms", parse.parse_vms, failed, []))
    stacks, loose = merge_stacks(stack_dirs, projects, containers)
    prev_cpu = previous.cpu_times if previous else None
    return Snapshot(
        array=array,
        disks=disks,
        shares=shares,
        cpu_times=cpu_times,
        cpu_percent=parse.cpu_percent(prev_cpu, cpu_times) if cpu_times else None,
        memory=memory,
        load=load,
        gpus=gpus,
        stacks=stacks,
        template_containers=loose,
        containers=tuple(containers),
        vms=vms,
        failed=frozenset(failed),
    )


def find_disk(snapshot: Snapshot, name: str) -> Disk | None:
    return next((d for d in snapshot.disks if d.name == name), None)


def find_share(snapshot: Snapshot, name: str) -> Share | None:
    return next((s for s in snapshot.shares if s.name == name), None)


def find_gpu(snapshot: Snapshot, index: int) -> Gpu | None:
    return next((g for g in snapshot.gpus if g.index == index), None)


def find_stack(snapshot: Snapshot, name: str) -> Stack | None:
    return next((s for s in snapshot.stacks if s.name == name), None)


def find_container(snapshot: Snapshot, name: str) -> Container | None:
    return next((c for c in snapshot.containers if c.name == name), None)


def find_vm(snapshot: Snapshot, name: str) -> Vm | None:
    return next((v for v in snapshot.vms if v.name == name), None)
