"""SSH setup/recovery, fingerprint consent, reconfiguration and options."""

from unittest.mock import AsyncMock

import pytest
from custom_components.unraid_ssh import config_flow as flow
from custom_components.unraid_ssh.ssh import (
    CommandResult,
    SSHAuthError,
    SSHConnectError,
    SSHHostKeyError,
    SSHKeyError,
    SSHTimeout,
    generate_keypair,
)
from pytest_homeassistant_custom_component.common import MockConfigEntry

ERRORS = [
    (SSHAuthError, "key_rejected"),
    (SSHConnectError, "cannot_connect"),
    (SSHTimeout, "cannot_connect"),
    (SSHHostKeyError, "host_key_changed"),
    (SSHKeyError, "key_unreadable"),
    (flow.NotUnraid, "not_unraid"),
]


@pytest.fixture
def keys():
    private, public = generate_keypair()
    _, host_key = generate_keypair()
    return {
        "host": "example.com",
        "port": 22,
        "name": "Example",
        "private_key": private,
        "public_key": public,
        "host_key": host_key,
        "scan_interval": 30,
    }


@pytest.fixture(autouse=True)
def no_entry_network(monkeypatch):
    monkeypatch.setattr(
        "custom_components.unraid_ssh.async_setup_entry", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(
        "custom_components.unraid_ssh.async_unload_entry", AsyncMock(return_value=True)
    )


async def begin(hass, monkeypatch, keys):
    monkeypatch.setattr(
        flow, "fetch_host_key", AsyncMock(return_value=keys["host_key"])
    )
    result = await hass.config_entries.flow.async_init(
        "unraid_ssh", context={"source": "user"}
    )
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": " example.com ", "port": 22, "name": " "}
    )
    assert result["step_id"] == "key"
    assert result["description_placeholders"]["public_key"].startswith("ssh-ed25519 ")
    return result


@pytest.mark.parametrize("exception,error", ERRORS)
async def test_key_error_then_success(hass, monkeypatch, keys, exception, error):
    result = await begin(hass, monkeypatch, keys)
    probe = AsyncMock(side_effect=exception("synthetic failure"))
    monkeypatch.setattr(flow, "validate", probe)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["errors"] == {"base": error}
    probe.side_effect = None
    probe.return_value = {"name": "Example", "version": "7.2"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] == "create_entry"
    assert result["data"]["host"] == "example.com"
    assert result["data"]["name"] == "Unraid SSH"
    assert result["result"].unique_id == "example.com:22"


@pytest.mark.parametrize("exception", [SSHConnectError, SSHTimeout])
async def test_host_error_then_success(hass, monkeypatch, keys, exception):
    probe = AsyncMock(side_effect=exception("offline"))
    monkeypatch.setattr(flow, "fetch_host_key", probe)
    result = await hass.config_entries.flow.async_init(
        "unraid_ssh", context={"source": "user"}
    )
    settings = {"host": "example.com", "port": 22, "name": "Example"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], settings)
    assert result["errors"] == {"base": "cannot_connect"}
    probe.side_effect = None
    probe.return_value = keys["host_key"]
    result = await hass.config_entries.flow.async_configure(result["flow_id"], settings)
    assert result["step_id"] == "key"


async def test_duplicate(hass, keys):
    MockConfigEntry(
        domain="unraid_ssh", unique_id="example.com:22", data=keys
    ).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        "unraid_ssh",
        context={"source": "user"},
        data={"host": "example.com", "port": 22, "name": "Example"},
    )
    assert result["reason"] == "already_configured"


@pytest.mark.parametrize("exception,error", ERRORS)
@pytest.mark.parametrize("source", ["reauth", "reconfigure"])
async def test_entry_flow_error_then_success(
    hass, monkeypatch, keys, exception, error, source
):
    entry = MockConfigEntry(domain="unraid_ssh", unique_id="example.com:22", data=keys)
    entry.add_to_hass(hass)
    _, offered = generate_keypair()
    monkeypatch.setattr(flow, "fetch_host_key", AsyncMock(return_value=offered))
    monkeypatch.setattr(
        hass.config_entries, "async_reload", AsyncMock(return_value=True)
    )
    probe = AsyncMock(side_effect=exception("synthetic failure"))
    monkeypatch.setattr(flow, "validate", probe)
    result = await hass.config_entries.flow.async_init(
        "unraid_ssh",
        context={"source": source, "entry_id": entry.entry_id},
        data=entry.data if source == "reauth" else None,
    )
    assert result["step_id"] == (
        "reauth_confirm" if source == "reauth" else "reconfigure"
    )
    settings = (
        {"accept_host_key": True}
        if source == "reauth"
        else {"host": "other.example.com", "port": 2222}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], settings)
    assert result["errors"] == {"base": error}
    probe.side_effect = None
    result = await hass.config_entries.flow.async_configure(result["flow_id"], settings)
    assert result["reason"] == (
        "reauth_successful" if source == "reauth" else "reconfigure_successful"
    )
    assert entry.data["host_key"] == offered
    assert entry.data["private_key"] == keys["private_key"]
    if source == "reconfigure":
        assert entry.unique_id == "other.example.com:2222"
        assert entry.data["host"] == "other.example.com"


async def test_reconfigure_duplicate(hass, keys):
    entry = MockConfigEntry(domain="unraid_ssh", unique_id="example.com:22", data=keys)
    entry.add_to_hass(hass)
    MockConfigEntry(
        domain="unraid_ssh",
        unique_id="other.example.com:22",
        data={**keys, "host": "other.example.com"},
    ).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        "unraid_ssh", context={"source": "reconfigure", "entry_id": entry.entry_id}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": "other.example.com", "port": 22}
    )
    assert result["reason"] == "already_configured"
    assert entry.unique_id == "example.com:22"


@pytest.mark.parametrize("accept", [False, True])
async def test_reauth_does_not_trust_unseen_key(hass, monkeypatch, keys, accept):
    entry = MockConfigEntry(domain="unraid_ssh", unique_id="example.com:22", data=keys)
    entry.add_to_hass(hass)
    monkeypatch.setattr(
        flow, "fetch_host_key", AsyncMock(side_effect=SSHConnectError("offline"))
    )

    async def validate(host, port, private, host_key):
        assert host_key == keys["host_key"]
        return {"name": "Example", "version": "7.2"}

    monkeypatch.setattr(flow, "validate", validate)
    monkeypatch.setattr(
        hass.config_entries, "async_reload", AsyncMock(return_value=True)
    )
    result = await hass.config_entries.flow.async_init(
        "unraid_ssh",
        context={"source": "reauth", "entry_id": entry.entry_id},
        data=entry.data,
    )
    assert result["description_placeholders"]["host_key_fingerprint"].startswith(
        "SHA256:"
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"accept_host_key": accept}
    )
    assert result["reason"] == "reauth_successful"
    assert entry.data["host_key"] == keys["host_key"]


@pytest.mark.parametrize("bad", ["public_key", "host_key"])
async def test_reauth_unreadable_key_renders(hass, monkeypatch, keys, bad):
    entry = MockConfigEntry(domain="unraid_ssh", data={**keys, bad: "invalid"})
    entry.add_to_hass(hass)
    monkeypatch.setattr(
        flow, "fetch_host_key", AsyncMock(side_effect=SSHTimeout("offline"))
    )
    result = await hass.config_entries.flow.async_init(
        "unraid_ssh",
        context={"source": "reauth", "entry_id": entry.entry_id},
        data=entry.data,
    )
    assert result["errors"] == {"base": "key_unreadable"}
    assert "host_key_fingerprint" in result["description_placeholders"]


async def test_key_unreadable_renders(hass, monkeypatch, keys):
    monkeypatch.setattr(
        flow, "fetch_host_key", AsyncMock(return_value=keys["host_key"])
    )
    monkeypatch.setattr(flow, "generate_keypair", lambda: ("invalid", "invalid"))
    result = await hass.config_entries.flow.async_init(
        "unraid_ssh",
        context={"source": "user"},
        data={"host": "example.com", "port": 22, "name": "Example"},
    )
    assert result["errors"] == {"base": "key_unreadable"}
    assert result["description_placeholders"]["public_key"] == ""


async def test_reconfigure_same_address(hass, monkeypatch, keys):
    entry = MockConfigEntry(domain="unraid_ssh", unique_id="example.com:22", data=keys)
    entry.add_to_hass(hass)
    monkeypatch.setattr(
        flow, "fetch_host_key", AsyncMock(return_value=keys["host_key"])
    )
    monkeypatch.setattr(flow, "validate", AsyncMock())
    monkeypatch.setattr(
        hass.config_entries, "async_reload", AsyncMock(return_value=True)
    )
    result = await hass.config_entries.flow.async_init(
        "unraid_ssh", context={"source": "reconfigure", "entry_id": entry.entry_id}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": "example.com", "port": 22}
    )
    assert result["reason"] == "reconfigure_successful"


async def test_options(hass, keys):
    entry = MockConfigEntry(
        domain="unraid_ssh",
        data=keys,
        options={"scan_interval": 60, "update_interval": 12},
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"scan_interval": 45, "update_interval": 24}
    )
    assert result["type"] == "create_entry"
    assert entry.options == {"scan_interval": 45, "update_interval": 24}


@pytest.mark.parametrize(
    "result,want",
    [
        (
            CommandResult(0, 'NAME="Example"\nversion="7.2"', ""),
            {"name": "Example", "version": "7.2"},
        ),
        (CommandResult(1, "", "error"), None),
        (CommandResult(0, "not unraid", ""), None),
    ],
)
async def test_validation_recognizes_unraid(monkeypatch, keys, result, want):
    monkeypatch.setattr(flow.UnraidSSH, "run", AsyncMock(return_value=result))
    if want is None:
        with pytest.raises(flow.NotUnraid):
            await flow.validate(
                "example.com", 22, keys["private_key"], keys["host_key"]
            )
    else:
        assert (
            await flow.validate(
                "example.com", 22, keys["private_key"], keys["host_key"]
            )
            == want
        )


async def test_reauth_consent_pins_displayed_key(hass, monkeypatch, keys):
    """A changed host key is trusted only after consent, and never refetched."""
    entry = MockConfigEntry(domain="unraid_ssh", unique_id="example.com:22", data=keys)
    entry.add_to_hass(hass)
    _, offered = generate_keypair()

    async def fetch_once(host, port):
        assert host == "example.com" and port == 22
        # A refetch after displaying the fingerprint would accept an unseen key.
        if fetch_once.called:
            raise AssertionError("Host key was fetched again after presentation")
        fetch_once.called = True
        return offered

    fetch_once.called = False
    monkeypatch.setattr(flow, "fetch_host_key", fetch_once)

    async def probe(host, port, private, host_key):
        assert private == keys["private_key"]
        if host_key == keys["host_key"]:
            raise SSHHostKeyError("changed")
        assert host_key == offered
        return {"name": "Example", "version": "7.2"}

    monkeypatch.setattr(flow, "validate", probe)
    monkeypatch.setattr(
        hass.config_entries, "async_reload", AsyncMock(return_value=True)
    )
    result = await hass.config_entries.flow.async_init(
        "unraid_ssh",
        context={"source": "reauth", "entry_id": entry.entry_id},
        data=entry.data,
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"accept_host_key": False}
    )
    assert result["errors"] == {"base": "host_key_changed"}
    assert entry.data["host_key"] == keys["host_key"]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"accept_host_key": True}
    )
    assert result["reason"] == "reauth_successful"
    assert entry.data["host_key"] == offered
