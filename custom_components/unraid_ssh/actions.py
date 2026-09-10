"""Fixed, shell-quoted commands for everything the integration can do.

Builders are pure functions (tested against exact strings). `run_action`
executes one over SSH and raises `ActionError` — with the tail of stderr, so
the reason is visible in Home Assistant instead of a silent no-op.
"""

from __future__ import annotations

import shlex

from .const import COMPOSE_SH
from .model import Stack
from .ssh import SSHError

_q = shlex.quote


class ActionError(Exception):
    """A command failed. `.stderr` is empty when this wraps an `SSHError`."""

    def __init__(self, message: str, stderr: str = "") -> None:
        super().__init__(message)
        self.stderr = stderr


# --- containers -------------------------------------------------------------------


def container_start_cmd(name: str) -> str:
    return f"docker start {_q(name)}"


def container_stop_cmd(name: str) -> str:
    return f"docker stop {_q(name)}"


# --- compose stacks -----------------------------------------------------------------


def _compose_manager_cmd(stack: Stack, verb: str) -> str:
    folder = stack.folder.rstrip("/")
    env = f"{folder}/.env"
    base = f"{COMPOSE_SH} -c {verb} -d {_q(folder)} -p {_q(stack.name)}"
    return f"if [ -f {_q(env)} ]; then {base} -e {_q(env)}; else {base}; fi"


def _plain_compose_cmd(stack: Stack, verb: str) -> str:
    # Built from parts and joined once: a global space collapse would also eat
    # a double space inside a quoted config-file path.
    parts = ["docker", "compose", "-p", _q(stack.name)]
    for path in stack.config_files:
        parts += ["-f", _q(path)]
    parts.append(verb)  # a literal ("up -d" / "down"), never user data
    return " ".join(parts)


def stack_up_cmd(stack: Stack) -> str:
    if stack.folder:
        return _compose_manager_cmd(stack, "up")
    return _plain_compose_cmd(stack, "up -d")


def stack_down_cmd(stack: Stack) -> str:
    if stack.folder:
        return _compose_manager_cmd(stack, "down")
    return _plain_compose_cmd(stack, "down")


# --- virtual machines ---------------------------------------------------------------


def vm_start_cmd(name: str) -> str:
    return f"virsh start {_q(name)}"


def vm_shutdown_cmd(name: str) -> str:
    return f"virsh shutdown {_q(name)}"


# --- runner ---------------------------------------------------------------------------


async def run_action(client, command: str, timeout: float) -> str:
    try:
        result = await client.run(command, timeout=timeout)
    except SSHError as err:
        raise ActionError(f"SSH: {err}") from err
    if result.exit_status != 0:
        tail = "\n".join(result.stderr.strip().splitlines()[-3:])
        raise ActionError(f"exit {result.exit_status}: {tail or 'no stderr'}", result.stderr)
    return result.stdout
