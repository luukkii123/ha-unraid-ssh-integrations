"""Home Assistant test fixtures for the custom integration."""

pytest_plugins = "pytest_homeassistant_custom_component"


import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Allow loading custom integrations in Home Assistant tests."""
    yield
