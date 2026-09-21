"""The snapshot one poll produces, assembled section by section.

Each section is parsed on its own: a parser that raises marks its section in
`Snapshot.failed` and leaves the others intact. Stacks come from the
compose.manager project folders (so a downed stack keeps existing) and are
enriched with `docker compose ls` and the container labels. A container whose
project label belongs to none of those still gets a stack of its own, so no
container is ever dropped on the floor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
from typing import Any, Callable

from . import parse
from .icons import IconSource, parse_icon_metadata
from .parse import (
    ArrayInfo,
    Container,
    CpuTimes,
    Disk,
    Gpu,
    HwSensor,
    Load,
    Memory,
    Share,
    Vm,
    sensor_key,
    temp_plausible,
)

_INVALID_PROJECT_CHARS = re.compile(r"[^a-z0-9_-]+")


def normalize_project_name(name: str) -> str:
    """Compose project-name rules: lowercase, [a-z0-9_-], no leading/trailing -/_.

    A run of invalid characters collapses into a single `-`, the way compose
    itself turns a folder like `Claude Station` into `claude-station`.
    """
    return _INVALID_PROJECT_CHARS.sub("-", name.strip().lower()).strip("-_")


_UNRAID_PROJECT_CHARS = re.compile(r"[.\s-]")


def unraid_project_name(name: str) -> str:
    """The project name Unraid's own Compose Manager starts a folder under.

    Not compose's rule -- the plugin's. `sanitizeStr()` in
    `/usr/local/emhttp/plugins/compose.manager/php/util.php` (lines 3-8)
    replaces `.`, space and `-` with `_` and lowercases the result; the string
    it is applied to comes from
    `/usr/local/emhttp/plugins/compose.manager/php/compose_util.php`
    (lines 47-51): the folder basename, replaced by the content of the folder's
    `name` file where that file exists -- which is exactly what
    `parse.StackDir.name` already holds. The result is handed to `compose.sh`
    as `-p`, so it is the name under which the stack comes up.

    Read off this server: the folder `gps-bridge` has no `name` file and runs
    as the project `gps_bridge`; `Claude-Station` has a `name` file saying
    `Claude Station` and would come up as `claude_station`.
    """
    return _UNRAID_PROJECT_CHARS.sub("_", name).lower()


@dataclass(frozen=True)
class Stack:
    name: str                  # the live project name while it runs, the fallback while it is down
    manager_name: str          # what Unraid would start it as, "" without a folder
    folder: str                # compose.manager project folder, "" when only seen in compose ls
    autostart: bool
    present: bool              # listed by `docker compose ls -a`
    running: int
    config_files: tuple[str, ...]
    containers: tuple[Container, ...]


def stack_key(stack: Stack) -> str:
    """The one name of a stack that does not move: its folder basename.

    `Stack.name` changes when the stack goes down (live project name -> derived
    name), so anything that has to stay put across a restart -- a device
    identifier, an entity's unique id -- keys on this instead. Only a stack
    without a folder has nothing but its project name, and that one cannot
    change: it exists only while compose reports it.
    """
    if stack.folder:
        return stack.folder.rstrip("/").rsplit("/", 1)[-1]
    return stack.name


def _match_projects(
    stack_dirs: list[parse.StackDir],
    projects: list[parse.ComposeProject],
) -> dict[int, parse.ComposeProject]:
    """Map folder index -> compose project.

    Three rules, in this order. First the compose-style name: the folder's
    `name` file, normalized, is often the project name outright. Then Unraid's
    own name -- `gps-bridge` on disk becomes the project `gps_bridge`, and that
    is what the Compose Manager itself would start it as. Only then the config
    files: a project started by hand from inside the folder is tied to it by
    that path alone. All name matches are resolved before any path match, so
    the order of the folders cannot let one folder take a project that another
    folder is actually named after; and a project claimed once is never
    claimed again.
    """
    matched: dict[int, parse.ComposeProject] = {}
    claimed: set[str] = set()
    for derive in (normalize_project_name, unraid_project_name):
        for index, folder in enumerate(stack_dirs):
            if index in matched:
                continue
            derived = derive(folder.name)
            for project in projects:
                if project.name not in claimed and project.name == derived:
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
    taken: set[str] = set()
    for index, folder in enumerate(stack_dirs):
        info = matched.get(index)
        # The compose project name is what `docker ps` labels and what every
        # compose command takes, so a matched stack carries it; an unmatched
        # folder falls back to its own normalized name.
        name = info.name if info else normalize_project_name(folder.name)
        taken.add(name)
        stacks.append(
            Stack(
                name=name,
                manager_name=unraid_project_name(folder.name),
                folder=folder.path,
                autostart=folder.autostart,
                present=info is not None,
                running=info.running if info else 0,
                config_files=info.config_files if info else (),
                containers=tuple(by_project.get(name, ())),
            )
        )
    for info in projects:
        if info.name in taken:
            continue
        taken.add(info.name)
        stacks.append(
            Stack(
                name=info.name, manager_name="", folder="", autostart=False, present=True,
                running=info.running, config_files=info.config_files,
                containers=tuple(by_project.get(info.name, ())),
            )
        )
    # Containers whose project label matches no stack we resolved: an orphan of a
    # removed or renamed project, or — the case that matters — every stack at once
    # when the `compose` section fails to parse while `docker` succeeds. They get a
    # synthetic stack instead of disappearing, so the device stays in place.
    for project_name, members in by_project.items():
        if project_name in taken:
            continue
        taken.add(project_name)
        stacks.append(
            Stack(
                name=project_name, manager_name="", folder="", autostart=False, present=False,
                running=sum(1 for c in members if c.state == "running"),
                config_files=(),
                containers=tuple(members),
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
    icon_sources: dict[str, IconSource] = field(default_factory=dict)
    icons_valid: bool = True
    # Last, and defaulted, only because dataclasses demand it: every field
    # above it is required, and the tests that build snapshots by hand
    # would all have to change for a channel list most of them do not use.
    sensors: tuple[HwSensor, ...] = ()


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
    sensors = tuple(_section(sections, "sensors", parse.parse_sensors, failed, []))
    containers = _section(sections, "docker", parse.parse_containers, failed, [])
    icon_sources = _section(sections, "icons", parse_icon_metadata, failed, {})
    icons_valid = "icons" not in failed
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
        sensors=sensors,
        stacks=stacks,
        template_containers=loose,
        containers=tuple(containers),
        vms=vms,
        failed=frozenset(failed),
        icon_sources=icon_sources,
        icons_valid=icons_valid,
    )


def find_disk(snapshot: Snapshot, name: str) -> Disk | None:
    return next((d for d in snapshot.disks if d.name == name), None)


def find_share(snapshot: Snapshot, name: str) -> Share | None:
    return next((s for s in snapshot.shares if s.name == name), None)


def find_gpu(snapshot: Snapshot, index: int) -> Gpu | None:
    return next((g for g in snapshot.gpus if g.index == index), None)


def find_sensor(snapshot: Snapshot, key: str) -> HwSensor | None:
    """By `parse.sensor_key` -- chip, device and channel, never the hwmon number."""
    return next((s for s in snapshot.sensors if sensor_key(s) == key), None)


#: Chip names mapped to something a dashboard can read. `Composite` and
#: `Sensor 1` mean nothing on their own; `NVMe Composite` does.
_CHIP_NAMES: tuple[tuple[str, str], ...] = (
    ("k10temp", "CPU"),
    ("coretemp", "CPU"),
    ("nvme", "NVMe"),
    ("drivetemp", "Disk"),
)

#: Prefixes of the Super-I/O chips that sit on the board itself. The exact
#: model varies per board (nct6798 here, nct6775/it8728 elsewhere), and the
#: number is no help to anyone reading a dashboard.
_BOARD_CHIPS: tuple[str, ...] = ("nct67", "nct61", "it87", "it86")


def is_board_chip(chip: str) -> bool:
    return chip.lower().startswith(_BOARD_CHIPS)


def chip_display_name(chip: str) -> str:
    """A readable chip name, or the raw one when the table has no entry."""
    lowered = chip.lower()
    if is_board_chip(lowered):
        return "Mainboard"
    return next((name for prefix, name in _CHIP_NAMES if lowered.startswith(prefix)), chip)


def _find_labelled(snapshot: Snapshot, chip: str, label: str) -> HwSensor | None:
    """One temperature channel, addressed the way a human would: chip and label."""
    for sensor in snapshot.sensors:
        if sensor.kind != "temp":
            continue
        matches_chip = is_board_chip(sensor.chip) if chip == "board" else sensor.chip.lower().startswith(chip)
        if matches_chip and (sensor.label == label or sensor.channel == label):
            return sensor
    return None


#: In order of trust. `Tctl` is what AMD itself reports to the fan control,
#: `Package id 0` is Intel's equivalent; `CPUTIN` is the board's own reading
#: of the socket and only a fallback, because some boards wire it elsewhere.
_CPU_TEMP_SOURCES: tuple[tuple[str, str], ...] = (
    ("k10temp", "Tctl"),
    ("k10temp", "Tdie"),
    ("coretemp", "Package id 0"),
    ("board", "CPUTIN"),
)
_BOARD_TEMP_SOURCES: tuple[tuple[str, str], ...] = (
    ("board", "SYSTIN"),
    ("acpitz", "temp1"),
)


def _first_temp(snapshot: Snapshot, sources: tuple[tuple[str, str], ...]) -> float | None:
    for chip, label in sources:
        sensor = _find_labelled(snapshot, chip, label)
        if sensor is not None and temp_plausible(sensor.value):
            return float(sensor.value)
    return None


def cpu_temp(snapshot: Snapshot) -> float | None:
    """The one CPU temperature a dashboard can bind to for good.

    The per-channel sensors carry the chip in their id, so a board swap or a
    move from AMD to Intel renames every one of them. This one does not: it
    picks the best source that exists right now and keeps its own id.
    """
    return _first_temp(snapshot, _CPU_TEMP_SOURCES)


def board_temp(snapshot: Snapshot) -> float | None:
    return _first_temp(snapshot, _BOARD_TEMP_SOURCES)


def stack_switchable(stack: Stack) -> bool:
    """False for a stack that has neither a folder nor a compose file.

    Such a stack exists only because its containers still carry the project
    label. `docker compose -p <name> down` works there (it goes by label and
    would remove them), but `up -d` cannot -- there is no configuration file to
    read. A switch would therefore be a trapdoor: switching off empties the
    project, `merge_stacks` finds no members left, the stack disappears from the
    snapshot, and neither the switch nor its containers can ever come back from
    Home Assistant. The stack device and the switches of its containers stay --
    they are the only handle left on those containers.
    """
    return bool(stack.folder or stack.config_files)


def container_updatable(container: Container, stack: Stack | None) -> bool:
    """True only where `actions.container_update_cmd` can actually work.

    Two update paths exist, and each has a precondition. A template container
    (no compose project, hence no service label) is updated by Unraid's own
    `update_container` script -- which knows template containers and nothing
    else. A compose container is updated with `docker compose ... pull <svc>`
    and `up -d <svc>`, using exactly the files its stack was started from;
    without a single `-f`, compose answers "no configuration file provided"
    (measured on this server). So a compose container whose stack has no config
    files -- a downed compose.manager folder, or an orphan whose project label
    matches no stack at all -- has no working path. Its update entity still
    shows the version comparison; only INSTALL is withheld, because a button
    that can only fail is worse than no button.
    """
    if not container.service:
        return True
    return stack is not None and bool(stack.config_files)


def find_stack(snapshot: Snapshot, name: str) -> Stack | None:
    """By project name -- what a container's `project` label gives you."""
    return next((s for s in snapshot.stacks if s.name == name), None)


def find_stack_by_key(snapshot: Snapshot, key: str) -> Stack | None:
    """By stable key -- what an entity created once has to keep looking up.

    An entity's item key is frozen when it is created, and `Stack.name` is not:
    a stack that was running as `gps_bridge` is called `gps-bridge` once it is
    down. Looking up by name would make the stack switch unavailable exactly
    when someone wants to switch it back on.
    """
    return next((s for s in snapshot.stacks if stack_key(s) == key), None)


def find_container(snapshot: Snapshot, name: str) -> Container | None:
    return next((c for c in snapshot.containers if c.name == name), None)


def find_vm(snapshot: Snapshot, name: str) -> Vm | None:
    return next((v for v in snapshot.vms if v.name == name), None)


# --- update inventory --------------------------------------------------------------


@dataclass(frozen=True)
class ImageStatus:
    container: str
    image_ref: str
    local_digest: str | None
    remote_digest: str | None
    update_available: bool | None      # None = remote unknown


def refs_to_check(inventory: list[parse.InventoryRow], image_digests: dict[str, tuple[str, ...]]) -> list[str]:
    """Every distinct reference worth a registry lookup, sorted for a stable command."""
    return sorted({row.image_ref for row in inventory if image_digests.get(row.image_id)})


def build_update_state(
    inventory: list[parse.InventoryRow],
    image_digests: dict[str, tuple[str, ...]],
    remote: dict[str, str | None],
) -> dict[str, ImageStatus]:
    out: dict[str, ImageStatus] = {}
    for row in inventory:
        local = image_digests.get(row.image_id) or ()
        if not local:
            continue                       # locally built image: nothing to compare against
        remote_digest = remote.get(row.image_ref)
        available = None if remote_digest is None else remote_digest not in local
        out[row.name] = ImageStatus(
            container=row.name,
            image_ref=row.image_ref,
            local_digest=local[0],
            remote_digest=remote_digest,
            update_available=available,
        )
    return out


# Metadata contract v1. Keys are opaque to consumers; config_entry_id is the
# instance namespace. Docker names remain runtime targets, never Compose IDs.
def container_identity(snapshot: Snapshot, container: Container) -> tuple[str, str]:
    stack = find_stack(snapshot, container.project) if container.project else None
    if not container.project:
        return container.name, "stable"
    if (not container.service or container.replica is None or stack is None
            or {"docker", "compose", "stacks"} & snapshot.failed):
        return container.name, "legacy"
    matches = [c for c in snapshot.containers if c.project == container.project
               and c.service == container.service and c.replica == container.replica]
    if len(matches) != 1:
        return container.name, "ambiguous"
    return "compose:" + json.dumps([stack_key(stack), container.service, container.replica],
                                  ensure_ascii=True, separators=(",", ":")), "stable"


def container_key(snapshot: Snapshot, container: Container) -> str:
    return container_identity(snapshot, container)[0]


def find_container_by_key(snapshot: Snapshot, key: str) -> Container | None:
    return next((c for c in snapshot.containers if container_key(snapshot, c) == key), None)


def container_metadata(snapshot: Snapshot, container: Container, entry_id: str, role: str) -> dict[str, Any]:
    key, status = container_identity(snapshot, container)
    stack = find_stack(snapshot, container.project) if container.project else None
    return {"kind": "container", "role": role, "config_entry_id": entry_id,
            "container_key": key, "container_name": container.name,
            "container_state": container.state, "image": container.image,
            "stack_key": stack_key(stack) if stack else None,
            "stack_name": container.project or None,
            "compose_service": container.service or None, "compose_replica": container.replica,
            "identity_status": status}


def stack_metadata(stack: Stack, entry_id: str, role: str) -> dict[str, Any]:
    return {"kind": "stack", "role": role, "config_entry_id": entry_id,
            "stack_key": stack_key(stack), "stack_name": stack.name,
            "running_containers": sum(c.state == "running" for c in stack.containers),
            "total_containers": len(stack.containers)}


def stack_restartable(stack: Stack) -> bool:
    return bool(stack.config_files)
