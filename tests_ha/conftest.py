"""Home Assistant test fixtures for the custom integration."""

pytest_plugins = "pytest_homeassistant_custom_component"


import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Allow loading custom integrations in Home Assistant tests."""
    yield


@pytest.fixture(autouse=True)
def forbid_real_ssh(monkeypatch):
    """No HA fixture can accidentally open a connection to a real server."""
    async def denied(*args, **kwargs):
        raise AssertionError('Real SSH connections are forbidden in tests_ha')
    monkeypatch.setattr('asyncssh.connect', denied)
