"""Pure parsers for the raw text the SSH command chain returns.

No Home Assistant, no asyncssh in here. Every function takes text and returns
plain dataclasses, so each one is testable against a recorded fixture.
"""

from __future__ import annotations

from dataclasses import dataclass
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
