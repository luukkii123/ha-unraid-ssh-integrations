"""The single command chain sent per poll, and the splitting of its output.

Every section is one standard command; failures are swallowed with
`2>/dev/null` so a missing `nvidia-smi` empties one section instead of
breaking the poll. The final `@@@ end` marker proves the output is complete.
"""

from __future__ import annotations

import re
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
    '{{.Label "com.docker.compose.project"}}\\t{{.Label "com.docker.compose.service"}}\\t'
    '{{json (.Label "net.unraid.docker.icon")}}'
)
_ICON_METADATA_PHP = r'''
$metadata = "/var/local/emhttp/plugins/dynamix.docker.manager/docker.json";
if (!is_file($metadata)) {
    echo "{}";
    exit(0);
}
$raw = @file_get_contents($metadata);
if ($raw === false) {
    exit(1);
}
$records = json_decode($raw, true);
if (!is_array($records) || json_last_error() !== JSON_ERROR_NONE) {
    exit(1);
}
$roots = [];
foreach ([
    "/usr/local/emhttp/state/plugins/dynamix.docker.manager/images",
    "/var/lib/docker/unraid/images",
] as $root) {
    $realRoot = realpath($root);
    if ($realRoot !== false) {
        $roots[] = rtrim($realRoot, DIRECTORY_SEPARATOR);
    }
}
$result = [];
foreach ($records as $name => $record) {
    if (!is_string($name) || $name === "" || !is_array($record)) {
        continue;
    }
    $icon = $record["icon"] ?? null;
    if (!is_string($icon) || ($icon = trim($icon)) === "") {
        continue;
    }
    if (strtolower(basename($icon)) === "question.png") {
        continue;
    }
    $candidate = str_starts_with($icon, "/var/lib/docker/unraid/images/")
        ? $icon
        : "/usr/local/emhttp/" . ltrim($icon, "/");
    $path = realpath($candidate);
    if ($path === false || !is_file($path)) {
        continue;
    }
    $allowed = false;
    foreach ($roots as $root) {
        if (str_starts_with($path, $root . DIRECTORY_SEPARATOR)) {
            $allowed = true;
            break;
        }
    }
    if (!$allowed) {
        continue;
    }
    $mtime = @filemtime($path);
    $size = @filesize($path);
    if ($mtime === false || $size === false) {
        continue;
    }
    $result[$name] = ["path" => $path, "revision" => $mtime . ":" . $size];
}
$json = json_encode($result, JSON_UNESCAPED_SLASHES);
if ($json === false) {
    exit(1);
}
echo $json;
'''.strip()
_ICON_METADATA_COMMAND = f"php -r {shlex.quote(_ICON_METADATA_PHP)}"
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
    ("icons", _ICON_METADATA_COMMAND),
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


#: A marker is `@@@ <name>` running to the end of its line. It is deliberately
#: NOT required to start one: `echo '@@@ next'` writes straight after whatever
#: the previous command left behind, so a section whose output has no final
#: newline (a file without one, a `head -1` of it) glues the next marker onto
#: its own last line. Matching only at line starts made that one missing byte
#: swallow every following section and fail the whole poll as truncated. The
#: name is restricted to lowercase words -- every section is one -- so no line
#: of real output can be mistaken for a marker.
_MARKER_RE = re.compile(re.escape(SECTION_MARKER) + r"([a-z_]+)[ \t]*(?:\n|\Z)")


def split_output(text: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    current: str | None = None
    content_start = 0
    for match in _MARKER_RE.finditer(text):
        if current is not None:
            sections[current] = text[content_start:match.start()]
        current = match.group(1)
        content_start = match.end()
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
