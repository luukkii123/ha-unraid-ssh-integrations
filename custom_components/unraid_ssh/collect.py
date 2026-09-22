"""The single command chain sent per poll, and the splitting of its output.

Every section carries its exit status; stderr stays private and failed sections
are discarded without breaking the poll. Bash pipefail preserves pipeline errors.
The final `@@@ end` marker proves the output is complete.
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

_GPU_QUERY = (
    "index,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,fan.speed"
)
_DOCKER_FORMAT = (
    '{{.Names}}\\t{{.State}}\\t{{.Image}}\\t'
    '{{.Label "com.docker.compose.project"}}\\t{{.Label "com.docker.compose.service"}}\\t'
    '{{json (.Label "net.unraid.docker.icon")}}\\t'
    '{{.Label "com.docker.compose.container-number"}}\\t'
    # Unraid's Docker manager stamps this on everything it owns, template
    # containers included. It is the only label that is always there: the icon
    # label is missing on most containers, so it cannot stand in for it. What
    # carries neither this nor a Compose project is a one-off `docker run`.
    '{{.Label "net.unraid.docker.managed"}}'
)
_ICON_METADATA_PATH = "/var/local/emhttp/plugins/dynamix.docker.manager/docker.json"
_ICON_EMHTTP_ROOT = "/usr/local/emhttp"
_ICON_CACHE_ROOTS = (
    "/usr/local/emhttp/state/plugins/dynamix.docker.manager/images",
    "/var/lib/docker/unraid/images",
)
_ICON_METADATA_PHP = r'''
$metadata = $argv[1] ?? null;
$emhttpRoot = $argv[2] ?? null;
$rootArguments = array_slice($argv, 3);
if (!is_string($metadata) || !is_string($emhttpRoot) || count($rootArguments) !== 2) {
    exit(1);
}
if (!is_file($metadata)) {
    echo "{}";
    exit(0);
}
$raw = @file_get_contents($metadata);
if ($raw === false) {
    exit(1);
}
$records = json_decode($raw);
if (!$records instanceof stdClass || json_last_error() !== JSON_ERROR_NONE) {
    exit(1);
}
$roots = [];
foreach ($rootArguments as $root) {
    $realRoot = realpath($root);
    if ($realRoot !== false) {
        $roots[] = rtrim($realRoot, DIRECTORY_SEPARATOR);
    }
}
$result = new stdClass();
foreach ($records as $name => $record) {
    $name = (string) $name;
    if ($name === "" || !$record instanceof stdClass) {
        continue;
    }
    $icon = $record->icon ?? null;
    if (!is_string($icon) || ($icon = trim($icon)) === "") {
        continue;
    }
    if (strtolower(basename($icon)) === "question.png") {
        continue;
    }
    $path = false;
    foreach ([rtrim($emhttpRoot, "/") . "/" . ltrim($icon, "/"), $icon] as $candidate) {
        $realCandidate = realpath($candidate);
        if ($realCandidate === false || !is_file($realCandidate)) {
            continue;
        }
        foreach ($roots as $root) {
            if (str_starts_with($realCandidate, $root . DIRECTORY_SEPARATOR)) {
                $path = $realCandidate;
                break 2;
            }
        }
    }
    if ($path === false) {
        continue;
    }
    $mtime = @filemtime($path);
    $size = @filesize($path);
    if ($mtime === false || $size === false) {
        continue;
    }
    $result->{$name} = (object) ["path" => $path, "revision" => $mtime . ":" . $size];
}
$json = json_encode($result, JSON_UNESCAPED_SLASHES);
if ($json === false) {
    exit(1);
}
echo $json;
'''.strip()


def build_icon_metadata_command(
    metadata_path: str = _ICON_METADATA_PATH,
    emhttp_root: str = _ICON_EMHTTP_ROOT,
    cache_roots: tuple[str, str] = _ICON_CACHE_ROOTS,
) -> str:
    """Build the fixed PHP reader with data paths passed only as quoted arguments."""

    if len(cache_roots) != 2:
        raise ValueError("exactly two icon cache roots are required")
    arguments = ("php", "-r", _ICON_METADATA_PHP, metadata_path, emhttp_root, *cache_roots)
    return " ".join(shlex.quote(argument) for argument in arguments)


_ICON_METADATA_COMMAND = build_icon_metadata_command()
#: Every temperature and fan channel the kernel exposes, one tab-separated row
#: each: chip, device, channel, label, value, pwm, pwm_enable. The mainboard
#: chip only appears once its driver is loaded (`modprobe nct6775` on this
#: board) -- the integration reads what is there and loads nothing itself.
#:
#: No `exit` anywhere in here, unlike `_STACKS_LOOP`: a single channel that
#: answers EIO -- which some Super-I/O chips do for unconnected inputs -- would
#: otherwise fail the whole section and take every other channel with it. A
#: channel that cannot be read is skipped instead, and the parser drops the
#: empty row.
_SENSORS_LOOP = (
    'for h in /sys/class/hwmon/hwmon*/; do '
    'n=$(cat "$h/name" 2>/dev/null) || continue; '
    'd=$(basename "$(readlink -f "$h/device" 2>/dev/null)" 2>/dev/null); '
    'for f in "$h"temp*_input "$h"fan*_input; do '
    '[ -e "$f" ] || continue; '
    'c=$(basename "$f" _input); '
    'v=$(cat "$f" 2>/dev/null) || continue; '
    'l=$(cat "${f%_input}_label" 2>/dev/null); '
    'p=; e=; '
    'case $c in fan*) i=${c#fan}; '
    'p=$(cat "$h/pwm$i" 2>/dev/null); e=$(cat "$h/pwm${i}_enable" 2>/dev/null);; esac; '
    "printf '%s\\t%s\\t%s\\t%s\\t%s\\t%s\\t%s\\n' "
    '"$n" "$d" "$c" "$l" "$v" "$p" "$e"; '
    'done; '
    'done'
)
_STACKS_LOOP = (
    f'for d in {COMPOSE_PROJECTS_DIR}/*/; do '
    '[ -d "$d" ] || continue; '
    'name=; autostart=; '
    'if [ -e "$d/name" ]; then name=$(cat "$d/name") || exit "$?"; fi; '
    'if [ -e "$d/autostart" ]; then autostart=$(cat "$d/autostart") || exit "$?"; fi; '
    "printf '%s\\t%s\\t%s\\n' \"$d\" \"$name\" \"$autostart\" || exit \"$?\"; "
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
    ("sensors", _SENSORS_LOOP),
    ("docker", f"docker ps -a --format '{_DOCKER_FORMAT}'"),
    ("icons", _ICON_METADATA_COMMAND),
    ("compose", "docker compose ls -a --format json"),
    ("stacks", _STACKS_LOOP),
    ("vms", "virsh list --all"),
)


class TruncatedOutput(Exception):
    """The end marker is missing — the output was cut off."""


def _build_sections(sections: tuple[tuple[str, str], ...]) -> str:
    """Isolate commands and report their status even after partial stdout."""
    parts = []
    for name, command in sections:
        parts.append(
            f"echo '{SECTION_MARKER}{name}'; "
            f"bash -o pipefail -c {shlex.quote(command)} 2>/dev/null; "
            f"printf '{SECTION_MARKER}{name} status=%s\\n' \"$?\""
        )
    parts.append(f"echo '{SECTION_MARKER}{END_SECTION}'")
    return "; ".join(parts)


def build_state_command() -> str:
    return _build_sections(SECTIONS)

#: A marker is `@@@ <name>` running to the end of its line. It is deliberately
#: NOT required to start one: `echo '@@@ next'` writes straight after whatever
#: the previous command left behind, so a section whose output has no final
#: newline (a file without one, a `head -1` of it) glues the next marker onto
#: its own last line. Matching only at line starts made that one missing byte
#: swallow every following section and fail the whole poll as truncated. The
#: name is restricted to lowercase words -- every section is one -- so no line
#: of real output can be mistaken for a marker.
_MARKER_RE = re.compile(re.escape(SECTION_MARKER) + r"([a-z_]+)(?: status=([0-9]+))?[ \t]*(?:\n|\Z)")


def split_output(text: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    current: str | None = None
    content_start = 0
    for match in _MARKER_RE.finditer(text):
        name, status = match.group(1), match.group(2)
        if status is not None:
            if current == name and status == "0":
                sections[name] = text[content_start:match.start()]
            else:
                sections.pop(name, None)
            current = None
        else:
            # Historical fixtures/inventory output have no status trailer.
            if current is not None:
                sections[current] = text[content_start:match.start()]
            current = name
        content_start = match.end()
    if current != END_SECTION:
        raise TruncatedOutput("output ended without the end marker")
    return sections


# --- update inventory ---------------------------------------------------------------

# `docker inspect --format` hands the string to Go's template engine untouched:
# unlike `docker ps --format`, it does NOT turn a literal `\t` into a tab, so the
# separator has to be a template action (`{{"\t"}}`). Recorded against docker
# 29.5.3 — with a plain `\t` every row came back as one field.
_CONTAINER_IDS = 'ids=$(docker ps -aq) || exit "$?"; [ -n "$ids" ] || exit 0; '
_INSPECT = (
    _CONTAINER_IDS + "docker inspect --format "
    "'{{.Name}}{{\"\\t\"}}{{.Config.Image}}{{\"\\t\"}}{{.Image}}' $ids"
)
_IMAGES = (
    _CONTAINER_IDS + "images=$(docker inspect --format '{{.Image}}' $ids | sort -u) || exit \"$?\"; "
    "docker image inspect --format '{{.Id}}{{\"\\t\"}}{{join .RepoDigests \",\"}}' $images"
)


def build_inventory_command() -> str:
    """Container -> image ref/id, plus the repo digests of every image in use."""
    return _build_sections((("containers", _INSPECT), ("images", _IMAGES)))


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
