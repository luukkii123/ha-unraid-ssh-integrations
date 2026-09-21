"""Pure parsers for the raw text the SSH command chain returns.

No Home Assistant, no asyncssh in here. Every function takes text and returns
plain dataclasses, so each one is testable against a recorded fixture.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Final

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
    fan_percent: int | None = None     # None: no fan, or nvidia-smi answered [N/A]


def _float(value: str, default: float = 0.0) -> float:
    try:
        return float(value.strip())
    except (TypeError, ValueError):
        return default


def parse_gpus(text: str) -> list[Gpu]:
    """`index, name, util, mem.used, mem.total, temp, power[, fan]` per GPU.

    Seven and eight columns both parse. The eighth (`fan.speed`, already a
    percentage) was added in 0.3.0, and a card without a controllable fan --
    every passively cooled or externally regulated one -- answers `[N/A]`
    there. That is an answer, not a failure: the column becomes `None` and no
    fan entity is created, while the other seven values stay usable.
    """
    gpus: list[Gpu] = []
    for line in text.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) not in (7, 8) or not parts[0].isdigit():
            continue                       # "No devices were found" and the like
        used, total = _int(parts[3]), _int(parts[4])
        fan = parts[7] if len(parts) == 8 else ""
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
                fan_percent=int(fan) if fan.isdigit() else None,
            )
        )
    return gpus


# --- /sys/class/hwmon (tab separated) ---------------------------------------------

#: Everything a hwmon channel key may contain. hwmon numbers (`hwmon3`) are
#: deliberately not part of any key: the kernel hands them out in probe order,
#: so the same chip is `hwmon1` today and `hwmon4` after the next boot -- and
#: every entity keyed on one would change its identity with it.
_SENSOR_KEY_CHARS = re.compile(r"[^a-z0-9_]+")

#: Temperatures outside this range are unconnected inputs, not measurements:
#: this board reports -59 °C on `AUXTIN2` and 0 °C on its four `PCH_*`
#: channels, none of which exist in hardware.
TEMP_MIN_CELSIUS: Final = 1.0
TEMP_MAX_CELSIUS: Final = 150.0


@dataclass(frozen=True)
class HwSensor:
    chip: str                  # hwmon `name`, e.g. k10temp / nct6798 / nvme
    device: str                # basename of the `device` link, e.g. 0000:00:18.3
    channel: str               # tempN / fanN, without the `_input` suffix
    label: str                 # tempN_label if the chip has one, else ""
    kind: str                  # "temp" or "fan"
    value: float | int         # °C for temp, RPM for fan
    pwm: int | None            # 0-255 drive level, fans only
    pwm_enable: int | None     # 5 = automatic curve, 1 = manual


def sensor_chip_key(sensor: HwSensor) -> str:
    """Chip name plus device basename, sanitized -- the stable half of a key.

    The chip name alone would collide the moment a second NVMe drive shows up:
    both report as `nvme`, and the two `temp1` channels would fight over one
    entity. The device basename is unique per chip instance and survives a
    reboot, which the hwmon number does not.
    """
    parts = [_SENSOR_KEY_CHARS.sub("_", part.lower()).strip("_") for part in (sensor.chip, sensor.device)]
    return "_".join(part for part in parts if part)


def sensor_key(sensor: HwSensor) -> str:
    """The identity of one channel: chip key plus channel name."""
    return f"{sensor_chip_key(sensor)}_{sensor.channel}"


def temp_plausible(value: float | int | None) -> bool:
    return value is not None and TEMP_MIN_CELSIUS <= value <= TEMP_MAX_CELSIUS


def _optional_int(value: str) -> int | None:
    return int(value) if value.lstrip("-").isdigit() else None


def parse_sensors(text: str) -> list[HwSensor]:
    """`chip \t device \t channel \t label \t value \t pwm \t pwm_enable`.

    Seven columns per line, written by the shell loop in `collect`. A channel
    the kernel refuses to read (EIO on some Super-I/O chips) arrives with an
    empty value column, and a chip that answers with something other than a
    number arrives as text; both are skipped, because a section that raised
    here would cost every other channel as well.
    """
    out: list[HwSensor] = []
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) != 7:
            continue
        chip, device, channel, label, raw, pwm, enable = (part.strip() for part in parts)
        kind = "temp" if channel.startswith("temp") else "fan" if channel.startswith("fan") else ""
        value = _optional_int(raw)
        if not chip or not channel or not kind or value is None:
            continue
        out.append(
            HwSensor(
                chip=chip,
                device=device,
                channel=channel,
                label=label,
                kind=kind,
                # Millidegrees from the kernel; one decimal is what the chips
                # actually resolve (0.5 °C steps on this board).
                value=round(value / 1000, 1) if kind == "temp" else value,
                pwm=_optional_int(pwm),
                pwm_enable=_optional_int(enable),
            )
        )
    return out


# --- docker ps (tab separated) ----------------------------------------------------


@dataclass(frozen=True)
class Container:
    name: str
    state: str                 # running / exited / created / paused / restarting / dead
    image: str
    project: str               # com.docker.compose.project label, "" for template containers
    service: str               # com.docker.compose.service label, "" for template containers
    icon: str | None = None     # decoded net.unraid.docker.icon label
    replica: int | None = None  # com.docker.compose.container-number, never inferred from name


def parse_containers(text: str) -> list[Container]:
    out: list[Container] = []
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) < 3 or not parts[0]:
            continue
        parts += [""] * (5 - len(parts))
        icon: str | None = None
        if len(parts) > 5:
            try:
                decoded = json.loads(parts[5])
            except (json.JSONDecodeError, TypeError):
                decoded = None
            if isinstance(decoded, str) and decoded.strip():
                icon = decoded.strip()
        out.append(
            Container(
                name=parts[0],
                state=parts[1],
                image=parts[2],
                project=parts[3],
                service=parts[4],
                icon=icon,
                replica=int(parts[6]) if len(parts) > 6 and parts[6].isascii() and parts[6].isdigit() and int(parts[6]) > 0 else None,
            )
        )
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
    rows = json.loads(text)
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("compose inventory must be a list of objects")
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


# --- update inventory --------------------------------------------------------------


@dataclass(frozen=True)
class InventoryRow:
    name: str                  # container name without the leading slash
    image_ref: str             # Config.Image as written in the template/compose file
    image_id: str              # sha256:… of the image the container runs


def parse_inventory(text: str) -> list[InventoryRow]:
    out: list[InventoryRow] = []
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        out.append(InventoryRow(name=parts[0].lstrip("/"), image_ref=parts[1], image_id=parts[2]))
    return out


def parse_image_digests(text: str) -> dict[str, tuple[str, ...]]:
    """`<image id>\\t<repo@sha256:…,repo2@sha256:…>` → id → digests (sha part only)."""
    out: dict[str, tuple[str, ...]] = {}
    for line in text.splitlines():
        image_id, _, rest = line.partition("\t")
        if not image_id:
            continue
        digests = tuple(ref.split("@", 1)[1] for ref in rest.split(",") if "@" in ref)
        out[image_id] = digests
    return out


def parse_remote_digests(text: str) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for line in text.splitlines():
        ref, _, digest = line.partition("\t")
        if ref:
            out[ref] = digest.strip() or None
    return out
