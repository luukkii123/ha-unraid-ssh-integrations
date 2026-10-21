"""Command builders are pure; the runner turns a non-zero exit into ActionError."""

from __future__ import annotations

import pytest

from unraid_ssh import actions
from unraid_ssh.model import Stack
from unraid_ssh.parse import Container
from unraid_ssh.ssh import CommandResult, SSHConnectError

FOLDER = "/boot/config/plugins/compose.manager/projects/Webstack/"
STACK = Stack(name="webstack", manager_name="webstack", folder=FOLDER, autostart=True, present=True,
              running=2, config_files=("/x/docker-compose.yml",), containers=())
ADHOC = Stack(name="adhoc", manager_name="", folder="", autostart=False, present=True, running=1,
              config_files=("/tmp/a.yml", "/tmp/b.yml"), containers=())
WEB = Container("webstack-web-1", "running", "webstack-web", "webstack", "web")
PREVIEW = Container("webstack-preview", "running", "nginx:alpine", "", "")


def test_container_commands_quote_names():
    assert actions.container_start_cmd("webstack-web-1") == "docker start webstack-web-1"
    assert actions.container_stop_cmd("odd name") == "docker stop 'odd name'"


def test_stack_up_uses_compose_manager_script_and_env_only_if_present():
    cmd = actions.stack_up_cmd(STACK)
    assert cmd.startswith("if [ -f /boot/config/plugins/compose.manager/projects/Webstack/.env ]; then ")
    assert "/usr/local/emhttp/plugins/compose.manager/scripts/compose.sh -c up -d /boot/config/plugins/compose.manager/projects/Webstack -p webstack -e /boot/config/plugins/compose.manager/projects/Webstack/.env" in cmd
    assert "; else /usr/local/emhttp/plugins/compose.manager/scripts/compose.sh -c up -d /boot/config/plugins/compose.manager/projects/Webstack -p webstack; fi" in cmd


def test_stack_up_uses_unraids_project_name_and_down_the_running_one():
    """Up is Unraid's button; down has to hit what is actually running.

    A folder called `net-bridge` is started by the Compose Manager as the
    project `net_bridge` (sanitizeStr in compose.manager/php/util.php). Using
    anything else for `up` would create a *second* project next to it: new
    network, new named volumes, new container names. `down`, on the other
    hand, must name the project that is running right now -- and while a stack
    runs, `Stack.name` is exactly that, straight out of `docker compose ls`.
    """
    gps = Stack(name="net-bridge", manager_name="net_bridge", folder="/p/net-bridge/", autostart=True,
                present=False, running=0, config_files=(), containers=())
    assert " -p net_bridge" in actions.stack_up_cmd(gps)
    assert " -p net-bridge" not in actions.stack_up_cmd(gps)

    running = Stack(name="net_bridge", manager_name="net_bridge", folder="/p/net-bridge/", autostart=True,
                    present=True, running=1, config_files=("/p/net-bridge/docker-compose.yml",), containers=())
    assert " -p net_bridge" in actions.stack_down_cmd(running)

    # Someone started the stack by hand under a name of their own: `down` still
    # has to name that one, not the one Unraid would have used.
    by_hand = Stack(name="stack-delta", manager_name="stack_delta", folder="/p/Stack-Delta/",
                    autostart=True, present=True, running=1,
                    config_files=("/p/Stack-Delta/docker-compose.yml",), containers=())
    assert " -p stack-delta" in actions.stack_down_cmd(by_hand)
    assert " -p stack_delta" in actions.stack_up_cmd(by_hand)


def test_stack_without_a_folder_uses_its_project_name_for_both():
    """No compose.manager folder, so no Unraid name exists -- `up` and `down`
    both go by the project name `docker compose ls` reports."""
    assert " -p adhoc " in actions.stack_up_cmd(ADHOC)
    assert " -p adhoc " in actions.stack_down_cmd(ADHOC)


def test_stack_down_uses_down_not_stop():
    cmd = actions.stack_down_cmd(STACK)
    assert "-c down" in cmd and "-c stop" not in cmd


def test_stack_without_folder_falls_back_to_compose_ls_files():
    assert actions.stack_up_cmd(ADHOC) == "docker compose -p adhoc -f /tmp/a.yml -f /tmp/b.yml up -d"
    assert actions.stack_down_cmd(ADHOC) == "docker compose -p adhoc -f /tmp/a.yml -f /tmp/b.yml down"


def test_vm_commands():
    assert actions.vm_start_cmd("Test VM 1") == "virsh start 'Test VM 1'"
    assert actions.vm_shutdown_cmd("Test VM 1") == "virsh shutdown 'Test VM 1'"
    assert actions.vm_force_stop_cmd("Guest's VM; echo nope") == "virsh destroy 'Guest'\"'\"'s VM; echo nope'"


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


def test_plain_compose_keeps_odd_paths_and_needs_no_space_collapse():
    """A double space inside a path must survive; an empty file list must not leave one."""
    odd = Stack(name="odd", manager_name="", folder="", autostart=False, present=True, running=0,
                config_files=("/tmp/a  b.yml",), containers=())
    assert "-f '/tmp/a  b.yml'" in actions.stack_up_cmd(odd)
    empty = Stack(name="odd", manager_name="", folder="", autostart=False, present=False, running=0,
                  config_files=(), containers=())
    assert actions.stack_down_cmd(empty) == "docker compose -p odd down"


def test_container_update_compose_pulls_and_recreates_one_service():
    """The optional --env-file rides in the positional parameters.

    `set --` leaves none, and `"$@"` then expands to zero words -- while a
    shell variable would have needed to stay unquoted to do the same, which
    would split any path with a space. Everything is shell-quoted; nothing in
    this command depends on a path being harmless.
    """
    cmd = actions.container_update_cmd(WEB, STACK)
    assert cmd == (
        "if [ -f /boot/config/plugins/compose.manager/projects/Webstack/.env ]; then "
        "set -- --env-file /boot/config/plugins/compose.manager/projects/Webstack/.env; "
        "else set --; fi && "
        "cd /boot/config/plugins/compose.manager/projects/Webstack && "
        'docker compose "$@" -p webstack -f /x/docker-compose.yml pull web && '
        'docker compose "$@" -p webstack -f /x/docker-compose.yml up -d web'
    )


def test_container_update_quotes_a_folder_with_a_space():
    """A compose.manager folder with a space must stay one word everywhere:
    in the `[ -f ... ]` test, in the `set --` line and in `cd`."""
    folder = "/mnt/user/appdata/Stack Delta/"
    stack = Stack(name="stack-delta", manager_name="stack_delta", folder=folder, autostart=True,
                  present=True, running=1,
                  config_files=("/mnt/user/appdata/Stack Delta/docker-compose.yml",), containers=())
    web = Container("stack-delta-web-1", "running", "img", "stack-delta", "web")
    assert actions.container_update_cmd(web, stack) == (
        "if [ -f '/mnt/user/appdata/Stack Delta/.env' ]; then "
        "set -- --env-file '/mnt/user/appdata/Stack Delta/.env'; "
        "else set --; fi && "
        "cd '/mnt/user/appdata/Stack Delta' && "
        'docker compose "$@" -p stack-delta '
        "-f '/mnt/user/appdata/Stack Delta/docker-compose.yml' pull web && "
        'docker compose "$@" -p stack-delta '
        "-f '/mnt/user/appdata/Stack Delta/docker-compose.yml' up -d web"
    )


def test_container_update_template_uses_unraid_script():
    assert actions.container_update_cmd(PREVIEW, None) == (
        "/usr/local/emhttp/plugins/dynamix.docker.manager/scripts/update_container webstack-preview"
    )


def test_container_update_without_a_folder_is_plain_compose():
    """No compose.manager folder: no .env to look for and nowhere to `cd` to.
    And, same reason as in `_plain_compose_cmd`, a double space inside a
    config-file path must survive -- the command is joined from parts."""
    odd = Stack(name="odd", manager_name="", folder="", autostart=False, present=True, running=1,
                config_files=("/tmp/a  b.yml",), containers=())
    web = Container("odd-web-1", "running", "img", "odd", "web")
    assert actions.container_update_cmd(web, odd) == (
        "docker compose -p odd -f '/tmp/a  b.yml' pull web && "
        "docker compose -p odd -f '/tmp/a  b.yml' up -d web"
    )
