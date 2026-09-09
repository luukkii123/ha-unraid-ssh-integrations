"""Pure parsers for the raw text the SSH command chain returns.

No Home Assistant, no asyncssh in here. Every function takes text and returns
plain dataclasses, so each one is testable against a recorded fixture.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re

_KV_RE = re.compile(r'^([A-Za-z0-9_.\-]+)="?(.*?)"?$')
_SECTION_RE = re.compile(r'^\["?([^"\]]+)"?\]$')


def parse_flat_ini(text: str) -> dict[str, str]:
    """`key="value"` lines without sections (var.ini)."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        match = _KV_RE.match(line.strip())
        if match:
            out[match.group(1)] = match.group(2)
    return out


def parse_sectioned_ini(text: str) -> dict[str, dict[str, str]]:
    """`["name"]` sections with `key="value"` lines (disks.ini, shares.ini)."""
    out: dict[str, dict[str, str]] = {}
    current: dict[str, str] | None = None
    for line in text.splitlines():
        stripped = line.strip()
        section = _SECTION_RE.match(stripped)
        if section:
            current = out.setdefault(section.group(1), {})
            continue
        match = _KV_RE.match(stripped)
        if match and current is not None:
            current[match.group(1)] = match.group(2)
    return out


def _int(value: str | None, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _yes(value: str | None) -> bool:
    return str(value or "").strip().lower() in ("yes", "1", "true")


# --- var.ini --------------------------------------------------------------------


@dataclass(frozen=True)
class ArrayInfo:
    name: str
    version: str
    md_state: str              # lowercased mdState: started / stopped / ...
    array_started: bool
    parity_running: bool
    parity_progress: float | None
    parity_errors: int
    mover_active: bool


def parse_var(text: str) -> ArrayInfo:
    var = parse_flat_ini(text)
    md_state = (var.get("mdState") or "unknown").lower()
    resync = _int(var.get("mdResync"))
    pos = _int(var.get("mdResyncPos"))
    running = resync > 0
    progress = round(pos / resync * 100, 1) if running else None
    return ArrayInfo(
        name=var.get("NAME") or "Unraid",
        version=var.get("version") or "",
        md_state=md_state,
        array_started=md_state == "started",
        parity_running=running,
        parity_progress=progress,
        parity_errors=_int(var.get("sbSyncErrs")),
        mover_active=_yes(var.get("shareMoverActive")),
    )


# --- disks.ini ------------------------------------------------------------------

_DISK_STATUS = {
    "DISK_OK": "ok",
    "DISK_NEW": "new",
    "DISK_DSBL": "disabled",
    "DISK_DSBL_NEW": "disabled",
    "DISK_INVALID": "invalid",
    "DISK_NP": "not_present",
    "DISK_NP_DSBL": "not_present",
    "DISK_NP_MISSING": "missing",
    "DISK_WRONG": "invalid",
}


def disk_status(raw: str) -> str:
    return _DISK_STATUS.get(raw.strip(), "unknown")


@dataclass(frozen=True)
class Disk:
    name: str
    device: str
    kind: str                  # Parity / Data / Cache / Flash (Unraid "type")
    status: str                # mapped, see disk_status()
    temp: int | None           # None when Unraid reports "*"
    spundown: bool
    fs_size_kib: int
    fs_free_kib: int
    usage_percent: float | None
    errors: int
    fs_status: str


def parse_disks(text: str) -> list[Disk]:
    disks: list[Disk] = []
    for name, sec in parse_sectioned_ini(text).items():
        device = sec.get("device", "")
        if not device:
            continue                       # empty slot
        raw_temp = sec.get("temp", "*").strip()
        temp = _int(raw_temp) if raw_temp.lstrip("-").isdigit() else None
        size = _int(sec.get("fsSize"))
        free = _int(sec.get("fsFree"))
        usage = round((size - free) / size * 100, 1) if size > 0 else None
        disks.append(
            Disk(
                name=sec.get("name") or name,
                device=device,
                kind=sec.get("type", ""),
                status=disk_status(sec.get("status", "")),
                temp=temp,
                spundown=sec.get("spundown", "0").strip() == "1",
                fs_size_kib=size,
                fs_free_kib=free,
                usage_percent=usage,
                errors=_int(sec.get("numErrors")),
                fs_status=sec.get("fsStatus", ""),
            )
        )
    return disks


# --- shares.ini -----------------------------------------------------------------


@dataclass(frozen=True)
class Share:
    name: str
    used_kib: int
    free_kib: int


def parse_shares(text: str) -> list[Share]:
    return [
        Share(name=sec.get("name") or name, used_kib=_int(sec.get("used")), free_kib=_int(sec.get("free")))
        for name, sec in parse_sectioned_ini(text).items()
    ]


# --- /proc ----------------------------------------------------------------------


@dataclass(frozen=True)
class CpuTimes:
    total: int
    idle: int


def parse_proc_stat(text: str) -> CpuTimes:
    """First line of /proc/stat: `cpu user nice system idle iowait irq softirq steal ...`."""
    for line in text.splitlines():
        parts = line.split()
        if parts and parts[0] == "cpu":
            values = [_int(p) for p in parts[1:]]
            idle = values[3] + (values[4] if len(values) > 4 else 0)   # idle + iowait
            return CpuTimes(total=sum(values), idle=idle)
    raise ValueError("no cpu line in /proc/stat output")


def cpu_percent(prev: CpuTimes | None, cur: CpuTimes) -> float | None:
    """Busy share between two samples; None on the first sample or no elapsed time."""
    if prev is None:
        return None
    total = cur.total - prev.total
    if total <= 0:
        return None
    idle = cur.idle - prev.idle
    return round((total - idle) / total * 100, 1)


@dataclass(frozen=True)
class Memory:
    total_kib: int
    available_kib: int
    percent: float


def parse_meminfo(text: str) -> Memory:
    values: dict[str, int] = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        values[key.strip()] = _int(rest.split()[0] if rest.split() else 0)
    total = values.get("MemTotal", 0)
    available = values.get("MemAvailable", 0)
    percent = round((total - available) / total * 100, 1) if total > 0 else 0.0
    return Memory(total_kib=total, available_kib=available, percent=percent)


@dataclass(frozen=True)
class Load:
    load1: float
    load5: float
    load15: float
    uptime_seconds: float


def parse_load(text: str) -> Load:
    """Two lines: /proc/loadavg then /proc/uptime."""
    lines = [l for l in text.splitlines() if l.strip()]
    loadavg = lines[0].split() if lines else []
    uptime = lines[1].split() if len(lines) > 1 else ["0"]
    return Load(
        load1=float(loadavg[0]) if loadavg else 0.0,
        load5=float(loadavg[1]) if len(loadavg) > 1 else 0.0,
        load15=float(loadavg[2]) if len(loadavg) > 2 else 0.0,
        uptime_seconds=float(uptime[0]),
    )


# --- nvidia-smi CSV -------------------------------------------------------------


@dataclass(frozen=True)
class Gpu:
    index: int
    name: str
    util_percent: float
    vram_used_mib: int
    vram_total_mib: int
    vram_percent: float
    temp: int
    power_w: float


def _float(value: str, default: float = 0.0) -> float:
    try:
        return float(value.strip())
    except (TypeError, ValueError):
        return default


def parse_gpus(text: str) -> list[Gpu]:
    """`index, name, util, mem.used, mem.total, temp, power` — one line per GPU."""
    gpus: list[Gpu] = []
    for line in text.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 7 or not parts[0].isdigit():
            continue                       # "No devices were found" and the like
        used, total = _int(parts[3]), _int(parts[4])
        gpus.append(
            Gpu(
                index=int(parts[0]),
                name=parts[1],
                util_percent=_float(parts[2]),
                vram_used_mib=used,
                vram_total_mib=total,
                vram_percent=round(used / total * 100, 1) if total > 0 else 0.0,
                temp=_int(parts[5]),
                power_w=_float(parts[6]),
            )
        )
    return gpus


# --- docker ps (tab separated) ----------------------------------------------------


@dataclass(frozen=True)
class Container:
    name: str
    state: str                 # running / exited / created / paused / restarting / dead
    image: str
    project: str               # com.docker.compose.project label, "" for template containers
    service: str               # com.docker.compose.service label, "" for template containers


def parse_containers(text: str) -> list[Container]:
    out: list[Container] = []
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) < 3 or not parts[0]:
            continue
        parts += [""] * (5 - len(parts))
        out.append(Container(name=parts[0], state=parts[1], image=parts[2], project=parts[3], service=parts[4]))
    return out


# --- docker compose ls (JSON) -------------------------------------------------------

_RUNNING_RE = re.compile(r"running\((\d+)\)")


@dataclass(frozen=True)
class ComposeProject:
    name: str
    status: str
    running: int
    config_files: tuple[str, ...]


def parse_compose_ls(text: str) -> list[ComposeProject]:
    if not text.strip():
        return []
    try:
        rows = json.loads(text)
    except json.JSONDecodeError:
        return []
    out: list[ComposeProject] = []
    for row in rows:
        status = str(row.get("Status") or "")
        match = _RUNNING_RE.search(status)
        files = tuple(f for f in str(row.get("ConfigFiles") or "").split(",") if f)
        out.append(
            ComposeProject(
                name=str(row.get("Name") or ""),
                status=status,
                running=int(match.group(1)) if match else 0,
                config_files=files,
            )
        )
    return out


# --- compose.manager project folders (tab separated) ----------------------------------


@dataclass(frozen=True)
class StackDir:
    path: str                  # folder with trailing slash, as printed by the shell loop
    name: str                  # content of the `name` file, else folder name
    autostart: bool


def parse_stack_dirs(text: str) -> list[StackDir]:
    out: list[StackDir] = []
    for line in text.splitlines():
        parts = line.split("\t")
        if not parts or not parts[0].strip():
            continue
        path = parts[0].strip()
        name = parts[1].strip() if len(parts) > 1 and parts[1].strip() else path.rstrip("/").rsplit("/", 1)[-1]
        autostart = _yes(parts[2]) if len(parts) > 2 else False
        out.append(StackDir(path=path, name=name, autostart=autostart))
    return out


# --- virsh list --all ------------------------------------------------------------------

_VM_STATES = {
    "running": "running",
    "shut off": "shut_off",
    "paused": "paused",
    "in shutdown": "in_shutdown",
    "crashed": "crashed",
    "pmsuspended": "pmsuspended",
    "idle": "idle",
}


def vm_state(raw: str) -> str:
    return _VM_STATES.get(raw.strip().lower(), "unknown")


@dataclass(frozen=True)
class Vm:
    name: str
    state: str


def parse_vms(text: str) -> list[Vm]:
    """Table with a header and a dashed line; columns are separated by 2+ spaces."""
    out: list[Vm] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("Id") or stripped.startswith("---"):
            continue
        parts = re.split(r"\s{2,}", stripped)
        if len(parts) < 3:
            continue
        out.append(Vm(name=parts[1], state=vm_state(parts[2])))
    return out
