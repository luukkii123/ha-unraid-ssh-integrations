"""The single command chain sent per poll, and the splitting of its output.

Every section is one standard command; failures are swallowed with
`2>/dev/null` so a missing `nvidia-smi` empties one section instead of
breaking the poll. The final `@@@ end` marker proves the output is complete.
"""

from __future__ import annotations

import shlex

from .const import (
    COMPOSE_PROJECTS_DIR,
    EMHTTP_DIR,
    END_SECTION,
    REMOTE_DIGEST_TIMEOUT_PER_IMAGE,
    SECTION_MARKER,
)

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


# --- update inventory ---------------------------------------------------------------

# `docker inspect --format` hands the string to Go's template engine untouched:
# unlike `docker ps --format`, it does NOT turn a literal `\t` into a tab, so the
# separator has to be a template action (`{{"\t"}}`). Recorded against docker
# 29.5.3 — with a plain `\t` every row came back as one field.
_INSPECT = (
    "docker inspect --format "
    "'{{.Name}}{{\"\\t\"}}{{.Config.Image}}{{\"\\t\"}}{{.Image}}' $(docker ps -aq)"
)
_IMAGES = (
    "docker image inspect --format '{{.Id}}{{\"\\t\"}}{{join .RepoDigests \",\"}}' "
    "$(docker ps -aq | xargs -r docker inspect --format '{{.Image}}' | sort -u)"
)


def build_inventory_command() -> str:
    """Container -> image ref/id, plus the repo digests of every image in use."""
    return (
        f"echo '{SECTION_MARKER}containers'; {_INSPECT} 2>/dev/null; "
        f"echo '{SECTION_MARKER}images'; {_IMAGES} 2>/dev/null; "
        f"echo '{SECTION_MARKER}{END_SECTION}'"
    )


def build_remote_digest_command(refs: list[str]) -> str:
    """One registry lookup per reference; a failing lookup leaves the column empty."""
    if not refs:
        return f"echo '{SECTION_MARKER}remote'; echo '{SECTION_MARKER}{END_SECTION}'"
    quoted = " ".join(shlex.quote(r) for r in refs)
    loop = (
        f"for r in {quoted}; do printf '%s\\t%s\\n' \"$r\" "
        f"\"$(timeout {REMOTE_DIGEST_TIMEOUT_PER_IMAGE} docker buildx imagetools inspect \"$r\" "
        "--format '{{.Manifest.Digest}}' 2>/dev/null)\"; done"
    )
    return f"echo '{SECTION_MARKER}remote'; {loop}; echo '{SECTION_MARKER}{END_SECTION}'"
