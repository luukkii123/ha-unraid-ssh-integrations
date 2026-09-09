"""The single command chain sent per poll, and the splitting of its output.

Every section is one standard command; failures are swallowed with
`2>/dev/null` so a missing `nvidia-smi` empties one section instead of
breaking the poll. The final `@@@ end` marker proves the output is complete.
"""

from __future__ import annotations

from .const import COMPOSE_PROJECTS_DIR, EMHTTP_DIR, END_SECTION, SECTION_MARKER

_GPU_QUERY = "index,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw"
_DOCKER_FORMAT = (
    '{{.Names}}\\t{{.State}}\\t{{.Image}}\\t'
    '{{.Label "com.docker.compose.project"}}\\t{{.Label "com.docker.compose.service"}}'
)
_STACKS_LOOP = (
    f'for d in {COMPOSE_PROJECTS_DIR}/*/; do '
    'printf \'%s\\t%s\\t%s\\n\' "$d" "$(cat "$d/name" 2>/dev/null)" "$(cat "$d/autostart" 2>/dev/null)"; '
    "done"
)

SECTIONS: tuple[tuple[str, str], ...] = (
    ("var", f"cat {EMHTTP_DIR}/var.ini"),
    ("disks", f"cat {EMHTTP_DIR}/disks.ini"),
    ("shares", f"cat {EMHTTP_DIR}/shares.ini"),
    ("stat", "head -1 /proc/stat"),
    ("mem", "grep -E '^(MemTotal|MemAvailable):' /proc/meminfo"),
    ("load", "cat /proc/loadavg /proc/uptime"),
    ("gpu", f"nvidia-smi --query-gpu={_GPU_QUERY} --format=csv,noheader,nounits"),
    ("docker", f"docker ps -a --format '{_DOCKER_FORMAT}'"),
    ("compose", "docker compose ls -a --format json"),
    ("stacks", _STACKS_LOOP),
    ("vms", "virsh list --all"),
)


class TruncatedOutput(Exception):
    """The end marker is missing — the output was cut off."""


def build_state_command() -> str:
    parts = [f"echo '{SECTION_MARKER}{name}'; {cmd} 2>/dev/null" for name, cmd in SECTIONS]
    parts.append(f"echo '{SECTION_MARKER}{END_SECTION}'")
    return "; ".join(parts)


def split_output(text: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    current: str | None = None
    buffer: list[str] = []
    for line in text.splitlines(keepends=True):
        if line.startswith(SECTION_MARKER):
            if current is not None:
                sections[current] = "".join(buffer)
            current = line[len(SECTION_MARKER):].strip()
            buffer = []
            continue
        buffer.append(line)
    if current != END_SECTION:
        raise TruncatedOutput("output ended without the end marker")
    return sections
