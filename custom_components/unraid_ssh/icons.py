"""Validate and select existing Unraid container icon sources."""

from __future__ import annotations

from dataclasses import dataclass
import json
import posixpath
import re
from urllib.parse import urlsplit

from .parse import Container

_REVISION_RE = re.compile(r"^[0-9]+:[0-9]+$")


@dataclass(frozen=True)
class IconSource:
    """One server file or remote URL which may provide a container icon."""

    kind: str
    value: str
    revision: str

    def __post_init__(self) -> None:
        if self.kind not in {"file", "url"}:
            raise ValueError("icon source kind must be 'file' or 'url'")


def _safe_cache_path(path: str) -> bool:
    # The fixed PHP reader already checked the path against both canonicalized
    # cache roots. Their real paths are host-dependent, so this boundary only
    # verifies that the emitted value stayed canonical and absolute.
    return path.startswith("/") and posixpath.normpath(path) == path


def parse_icon_metadata(text: str) -> dict[str, IconSource]:
    """Parse the selected, canonicalized subset emitted by the PHP reader."""

    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("icon metadata must be a JSON object")

    sources: dict[str, IconSource] = {}
    for name, record in data.items():
        if not isinstance(name, str) or not name or not isinstance(record, dict):
            continue
        path = record.get("path")
        revision = record.get("revision")
        if not isinstance(path, str) or not isinstance(revision, str):
            continue
        if (
            not _safe_cache_path(path)
            or posixpath.basename(path).casefold() == "question.png"
            or not _REVISION_RE.fullmatch(revision)
        ):
            continue
        sources[name] = IconSource("file", path, revision)
    return sources


def _valid_icon_url(value: str | None) -> str | None:
    if not isinstance(value, str) or not (value := value.strip()):
        return None
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
    except ValueError:
        return None
    if parsed.scheme.casefold() not in {"http", "https"} or not hostname:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    return value


def select_icon(
    container: Container,
    metadata: dict[str, IconSource],
) -> IconSource | None:
    """Prefer Unraid's cache file, then the container's effective URL label."""

    if source := metadata.get(container.name):
        return source
    if url := _valid_icon_url(container.icon):
        return IconSource("url", url, "")
    return None


MAX_ICON_BYTES = 1024 * 1024
MAX_ICON_PIXELS = 4 * 1024 * 1024
_ICON_READ_PHP = r'''
$path = @realpath($argv[1] ?? "");
if ($path === false || !is_file($path) || !is_readable($path)) { exit(1); }
$allowed = false;
foreach (array_slice($argv, 2) as $candidate) {
    $root = @realpath($candidate);
    if ($root !== false && is_dir($root) &&
        str_starts_with($path, rtrim($root, DIRECTORY_SEPARATOR) . DIRECTORY_SEPARATOR)) {
        $allowed = true;
    }
}
if (!$allowed) { exit(1); }
$size = @filesize($path);
if ($size === false || $size < 1 || $size > 1048576) { exit(1); }
$data = @file_get_contents($path, false, null, 0, 1048577);
if ($data === false || strlen($data) < 1 || strlen($data) > 1048576) { exit(1); }
echo base64_encode($data);
'''.strip()


def build_icon_read_command(path: str) -> str:
    """Read at most one MiB, with all shell data passed as separate arguments."""
    import shlex

    from .collect import _ICON_CACHE_ROOTS

    return shlex.join(('php', '-r', _ICON_READ_PHP, path, *_ICON_CACHE_ROOTS))


# Deliberately inert SVG: geometry and solid paint only. No style, href, use,
# image, animations, filters, foreign namespaces or processing instructions.
_SVG_ELEMENTS = {'svg', 'g', 'path', 'rect', 'circle', 'ellipse', 'line', 'polyline', 'polygon', 'title', 'desc'}
_SVG_ATTRIBUTES = {
    'viewBox', 'width', 'height', 'x', 'y', 'x1', 'x2', 'y1', 'y2', 'cx', 'cy',
    'r', 'rx', 'ry', 'd', 'points', 'fill', 'stroke', 'stroke-width', 'opacity',
    'fill-opacity', 'stroke-opacity', 'fill-rule', 'clip-rule', 'stroke-linecap',
    'stroke-linejoin', 'stroke-miterlimit', 'stroke-dasharray', 'stroke-dashoffset',
    'transform', 'preserveAspectRatio', 'version',
}
_SVG_NS = '{http://www.w3.org/2000/svg}'


def validate_icon(data: bytes) -> tuple[bytes, str] | None:
    """Decode bounded rasters or serialize a strict, resource-free SVG subset."""
    from io import BytesIO
    import warnings
    from xml.etree import ElementTree as ET

    from PIL import Image, UnidentifiedImageError

    if not 0 < len(data) <= MAX_ICON_BYTES:
        return None
    if data.lstrip().startswith(b'<'):
        # UTF-8 only makes the lexical DTD/PI boundary unambiguous.
        try:
            text = data.decode('utf-8')
            if '<!' in text or '<?' in text or '\x00' in text:
                return None
            root = ET.fromstring(text)
            if root.tag != _SVG_NS + 'svg':
                return None
            for index, node in enumerate(root.iter()):
                if index >= 4096 or node.tag not in {_SVG_NS + tag for tag in _SVG_ELEMENTS}:
                    return None
                for key, value in node.attrib.items():
                    if (key not in _SVG_ATTRIBUTES or 'url' in value.casefold()
                            or not re.fullmatch(r'[A-Za-z0-9\s.,#+()%\-]*', value)):
                        return None
            output = ET.tostring(root, encoding='utf-8')
            return (output, 'svg') if len(output) <= MAX_ICON_BYTES else None
        except (UnicodeError, ET.ParseError, ValueError):
            return None
    formats = {'PNG': 'png', 'JPEG': 'jpg', 'GIF': 'gif', 'WEBP': 'webp'}
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(BytesIO(data), formats=list(formats)) as image:
                extension = formats[image.format]
                if image.width * image.height > MAX_ICON_PIXELS:
                    return None
                image.verify()
            # verify() alone does not decompress pixels (or validate later frames).
            with Image.open(BytesIO(data), formats=list(formats)) as image:
                pixels = 0
                for frame in range(getattr(image, 'n_frames', 1)):
                    image.seek(frame)
                    pixels += image.width * image.height
                    if pixels > MAX_ICON_PIXELS:
                        return None
                    image.load()
            return data, extension
    except (OSError, ValueError, SyntaxError, UnidentifiedImageError,
            Image.DecompressionBombError, Image.DecompressionBombWarning):
        return None
