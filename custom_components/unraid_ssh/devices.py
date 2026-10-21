"""Stable suffixes shared by device-producing entity platforms."""

from __future__ import annotations

from .model import Snapshot, find_stack, stack_key
from .parse import Container


def container_assignment_complete(snapshot: Snapshot, container: Container | None) -> bool:
    """Project members need stack metadata to resolve their stable folder ID."""
    return "docker" not in snapshot.failed and (
        (container is not None and not container.project)
        or not {"compose", "stacks"} & snapshot.failed
    )


def container_device_suffix(snapshot: Snapshot, container: Container) -> str:
    """Return the stable child-device suffix for a container."""
    if not container.project:
        return f"container_{container.name}"
    stack = find_stack(snapshot, container.project)
    return f"stack_{stack_key(stack) if stack else container.project}"


def share_device_suffix(name: str) -> str:
    """Keep the complete share name in its child-device suffix."""
    return f"share_{name}"
