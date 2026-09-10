"""Command builders are pure; the runner turns a non-zero exit into ActionError."""

from __future__ import annotations

import pytest

from unraid_ssh import actions
from unraid_ssh.model import Stack
from unraid_ssh.ssh import CommandResult, SSHConnectError

FOLDER = "/boot/config/plugins/compose.manager/projects/Buschfunk/"
STACK = Stack(name="buschfunk", folder=FOLDER, autostart=True, present=True, running=2,
              config_files=("/x/docker-compose.yml",), containers=())
ADHOC = Stack(name="adhoc", folder="", autostart=False, present=True, running=1,
              config_files=("/tmp/a.yml", "/tmp/b.yml"), containers=())


def test_container_commands_quote_names():
    assert actions.container_start_cmd("buschfunk-web-1") == "docker start buschfunk-web-1"
    assert actions.container_stop_cmd("odd name") == "docker stop 'odd name'"


def test_stack_up_uses_compose_manager_script_and_env_only_if_present():
    cmd = actions.stack_up_cmd(STACK)
    assert cmd.startswith("if [ -f /boot/config/plugins/compose.manager/projects/Buschfunk/.env ]; then ")
    assert "/usr/local/emhttp/plugins/compose.manager/scripts/compose.sh -c up -d /boot/config/plugins/compose.manager/projects/Buschfunk -p buschfunk -e /boot/config/plugins/compose.manager/projects/Buschfunk/.env" in cmd
    assert "; else /usr/local/emhttp/plugins/compose.manager/scripts/compose.sh -c up -d /boot/config/plugins/compose.manager/projects/Buschfunk -p buschfunk; fi" in cmd


def test_stack_down_uses_down_not_stop():
    cmd = actions.stack_down_cmd(STACK)
    assert "-c down" in cmd and "-c stop" not in cmd


def test_stack_without_folder_falls_back_to_compose_ls_files():
    assert actions.stack_up_cmd(ADHOC) == "docker compose -p adhoc -f /tmp/a.yml -f /tmp/b.yml up -d"
    assert actions.stack_down_cmd(ADHOC) == "docker compose -p adhoc -f /tmp/a.yml -f /tmp/b.yml down"


def test_vm_commands():
    assert actions.vm_start_cmd("Windows 11") == "virsh start 'Windows 11'"
    assert actions.vm_shutdown_cmd("Windows 11") == "virsh shutdown 'Windows 11'"


class _Client:
    def __init__(self, result=None, error=None):
        self.result, self.error, self.calls = result, error, []

    async def run(self, command, timeout):
        self.calls.append((command, timeout))
        if self.error:
            raise self.error
        return self.result


@pytest.mark.asyncio
async def test_run_action_returns_stdout():
    client = _Client(CommandResult(0, "ok\n", ""))
    assert await actions.run_action(client, "true", 5) == "ok\n"
    assert client.calls == [("true", 5)]


@pytest.mark.asyncio
async def test_run_action_raises_on_nonzero_exit_with_stderr_tail():
    client = _Client(CommandResult(1, "", "line1\nError response from daemon: no such container\n"))
    with pytest.raises(actions.ActionError) as info:
        await actions.run_action(client, "docker start nope", 5)
    assert "no such container" in str(info.value)


@pytest.mark.asyncio
async def test_run_action_wraps_ssh_errors():
    client = _Client(error=SSHConnectError("down"))
    with pytest.raises(actions.ActionError):
        await actions.run_action(client, "true", 5)
