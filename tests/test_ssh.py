"""Transport against an in-process asyncssh server (no real Unraid needed)."""

from __future__ import annotations

import asyncio

import asyncssh
import pytest

from unraid_ssh import ssh as transport


class _FakeServer(asyncssh.SSHServer):
    def __init__(self, allowed_public_key: str) -> None:
        self._allowed = asyncssh.import_authorized_keys(allowed_public_key + "\n")

    def begin_auth(self, username: str) -> bool:
        return True

    def public_key_auth_supported(self) -> bool:
        return True

    def validate_public_key(self, username: str, key: asyncssh.SSHKey) -> bool:
        return self._allowed.validate(key, "127.0.0.1", "127.0.0.1") is not None


async def _start(allowed_public_key: str, handler):
    host_key = asyncssh.generate_private_key("ssh-ed25519")

    async def process(proc: asyncssh.SSHServerProcess) -> None:
        exit_status = await handler(proc)
        proc.exit(exit_status)

    server = await asyncssh.create_server(
        lambda: _FakeServer(allowed_public_key), "127.0.0.1", 0,
        server_host_keys=[host_key], process_factory=process,
    )
    port = server.sockets[0].getsockname()[1]
    host_key_line = host_key.export_public_key("openssh").decode().strip()
    return server, port, host_key_line


@pytest.fixture
def keypair():
    return transport.generate_keypair()


def test_generate_keypair_shapes(keypair):
    private, public = keypair
    assert "PRIVATE KEY" in private
    assert public.startswith("ssh-ed25519 ")
    assert public.endswith(" unraid_ssh@homeassistant")
    assert transport.fingerprint(public).startswith("SHA256:")


@pytest.mark.asyncio
async def test_run_returns_stdout_and_status(keypair):
    private, public = keypair
    seen: list[str] = []

    async def handler(proc):
        seen.append(proc.command)
        proc.stdout.write("hello\n")
        proc.stderr.write("warn\n")
        return 3

    server, port, host_key = await _start(public, handler)
    try:
        client = transport.UnraidSSH("127.0.0.1", port, private, host_key)
        result = await client.run("echo hello", timeout=5)
    finally:
        server.close()
        await server.wait_closed()
    assert seen == ["echo hello"]
    assert result.exit_status == 3
    assert result.stdout == "hello\n"
    assert result.stderr == "warn\n"


@pytest.mark.asyncio
async def test_wrong_key_raises_auth_error(keypair):
    private, public = keypair
    other_private, _ = transport.generate_keypair()

    async def handler(proc):
        return 0

    server, port, host_key = await _start(public, handler)
    try:
        client = transport.UnraidSSH("127.0.0.1", port, other_private, host_key)
        with pytest.raises(transport.SSHAuthError):
            await client.run("true", timeout=5)
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_wrong_host_key_raises_host_key_error(keypair):
    private, public = keypair

    async def handler(proc):
        return 0

    server, port, _ = await _start(public, handler)
    wrong = asyncssh.generate_private_key("ssh-ed25519").export_public_key("openssh").decode().strip()
    try:
        client = transport.UnraidSSH("127.0.0.1", port, private, wrong)
        with pytest.raises(transport.SSHHostKeyError):
            await client.run("true", timeout=5)
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_fetch_host_key_matches_server(keypair):
    _, public = keypair

    async def handler(proc):
        return 0

    server, port, host_key = await _start(public, handler)
    try:
        assert await transport.fetch_host_key("127.0.0.1", port) == host_key
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_closed_port_raises_connect_error(keypair):
    private, _ = keypair
    client = transport.UnraidSSH("127.0.0.1", 1, private, "ssh-ed25519 AAAA")
    with pytest.raises(transport.SSHConnectError):
        await client.run("true", timeout=5)


@pytest.mark.asyncio
async def test_slow_command_raises_timeout(keypair):
    private, public = keypair

    async def handler(proc):
        await asyncio.sleep(5)
        return 0

    server, port, host_key = await _start(public, handler)
    try:
        client = transport.UnraidSSH("127.0.0.1", port, private, host_key)
        with pytest.raises(transport.SSHTimeout):
            await client.run("sleep 5", timeout=0.5)
    finally:
        server.close()
        await server.wait_closed()
