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
