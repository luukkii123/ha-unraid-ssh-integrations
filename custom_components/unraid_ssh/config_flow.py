"""Config flow: host → generated key shown to the user → connection test.

No password anywhere. The flow generates an ed25519 key pair, shows the
public half with the two lines to paste on Unraid, and only creates the
entry once a real command has run over SSH with that key.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    AUTHORIZED_KEYS_FILE,
    CONF_HOST,
    CONF_HOST_KEY,
    CONF_NAME,
    CONF_PORT,
    CONF_PRIVATE_KEY,
    CONF_PUBLIC_KEY,
    CONF_SCAN_INTERVAL,
    CONF_UPDATE_INTERVAL,
    DEFAULT_NAME,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    EMHTTP_DIR,
    MAX_SCAN_INTERVAL,
    MAX_UPDATE_INTERVAL,
    MIN_SCAN_INTERVAL,
    MIN_UPDATE_INTERVAL,
    POLL_TIMEOUT,
    PUBKEYS_FILE,
)
from .parse import parse_var
from .ssh import (
    SSHAuthError,
    SSHConnectError,
    SSHHostKeyError,
    SSHKeyError,
    SSHTimeout,
    UnraidSSH,
    fetch_host_key,
    fingerprint,
    generate_keypair,
)

CONF_ACCEPT_HOST_KEY = "accept_host_key"


class NotUnraid(Exception):
    """The command ran, but var.ini did not look like Unraid."""


def _user_schema() -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_HOST): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
            vol.Required(CONF_PORT, default=DEFAULT_PORT): NumberSelector(
                NumberSelectorConfig(min=1, max=65535, mode=NumberSelectorMode.BOX)
            ),
            vol.Required(CONF_NAME, default=DEFAULT_NAME): TextSelector(
                TextSelectorConfig(type=TextSelectorType.TEXT)
            ),
        }
    )


def _key_placeholders(public_key: str, host: str) -> dict[str, str]:
    return {
        "public_key": public_key,
        "fingerprint": fingerprint(public_key),
        "host": host,
        "pubkeys_line": f"echo '{public_key}' >> {PUBKEYS_FILE}",
        "authorized_line": f"echo '{public_key}' >> {AUTHORIZED_KEYS_FILE}",
    }


def _blank_placeholders(host: str) -> dict[str, str]:
    """Placeholders for a form that still has to render with an unreadable key.

    Every name the step description substitutes must exist, otherwise the form
    cannot be shown at all and the user never sees the error message.
    """
    return {
        "public_key": "",
        "fingerprint": "",
        "host": host,
        "pubkeys_line": "",
        "authorized_line": "",
    }


async def validate(host: str, port: int, private_key: str, host_key: str) -> dict[str, str]:
    """Run one real command with the key; return name and version from var.ini."""
    client = UnraidSSH(host, port, private_key, host_key)
    result = await client.run(f"cat {EMHTTP_DIR}/var.ini", timeout=POLL_TIMEOUT)
    if result.exit_status != 0 or 'NAME="' not in result.stdout:
        raise NotUnraid(result.stderr.strip()[-200:])
    info = parse_var(result.stdout)
    return {"name": info.name, "version": info.version}


class UnraidConfigFlow(ConfigFlow, domain=DOMAIN):
    """user → key → entry; reauth; reconfigure."""

    VERSION = 1

    def __init__(self) -> None:
        self._host = ""
        self._port = DEFAULT_PORT
        self._name = DEFAULT_NAME
        self._private_key = ""
        self._public_key = ""
        self._host_key = ""
        #: Host key fetched when the reauth form was first rendered — the one
        #: whose fingerprint the user is looking at while ticking the box.
        self._offered_host_key: str | None = None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._host = str(user_input[CONF_HOST]).strip()
            self._port = int(user_input[CONF_PORT])
            self._name = str(user_input[CONF_NAME]).strip() or DEFAULT_NAME
            await self.async_set_unique_id(f"{self._host}:{self._port}")
            self._abort_if_unique_id_configured()
            try:
                self._host_key = await fetch_host_key(self._host, self._port)
            except (SSHConnectError, SSHTimeout):
                errors["base"] = "cannot_connect"
            else:
                self._private_key, self._public_key = generate_keypair()
                return await self.async_step_key()
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(_user_schema(), user_input),
            errors=errors,
        )

    async def async_step_key(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show the public key; submitting runs the connection test."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                info = await validate(self._host, self._port, self._private_key, self._host_key)
            except SSHAuthError:
                errors["base"] = "key_rejected"
            except (SSHConnectError, SSHTimeout):
                errors["base"] = "cannot_connect"
            except SSHHostKeyError:
                errors["base"] = "host_key_changed"
            except SSHKeyError:
                errors["base"] = "key_unreadable"
            except NotUnraid:
                errors["base"] = "not_unraid"
            else:
                return self.async_create_entry(
                    title=self._name,
                    data={
                        CONF_HOST: self._host,
                        CONF_PORT: self._port,
                        CONF_NAME: self._name,
                        CONF_PRIVATE_KEY: self._private_key,
                        CONF_PUBLIC_KEY: self._public_key,
                        CONF_HOST_KEY: self._host_key,
                        CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
                    },
                    description_placeholders={"server": info["name"], "version": info["version"]},
                )
        try:
            placeholders = _key_placeholders(self._public_key, self._host)
        except SSHKeyError:
            placeholders = _blank_placeholders(self._host)
            errors["base"] = "key_unreadable"
        return self.async_show_form(
            step_id="key",
            data_schema=vol.Schema({}),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Key rejected or host key changed: show the same public key again."""
        entry = self._get_reauth_entry()
        host, port = entry.data[CONF_HOST], int(entry.data.get(CONF_PORT, DEFAULT_PORT))
        errors: dict[str, str] = {}
        if user_input is None:
            # Fetch exactly once, while the form is being built. Ticking the box
            # must trust the key whose fingerprint the user was shown, not
            # whatever the server offers a minute later.
            try:
                self._offered_host_key = await fetch_host_key(host, port)
            except (SSHConnectError, SSHTimeout):
                self._offered_host_key = None
        offered = self._offered_host_key

        try:
            placeholders = _key_placeholders(entry.data[CONF_PUBLIC_KEY], host)
        except SSHKeyError:
            placeholders = _blank_placeholders(host)
            errors["base"] = "key_unreadable"
        placeholders["new_fingerprint"] = ""
        if offered is not None and offered != entry.data[CONF_HOST_KEY]:
            try:
                placeholders["new_fingerprint"] = fingerprint(offered)
            except SSHKeyError:
                errors["base"] = "key_unreadable"

        if user_input is not None:
            host_key = entry.data[CONF_HOST_KEY]
            if user_input.get(CONF_ACCEPT_HOST_KEY) and offered is not None:
                host_key = offered
            try:
                await validate(host, port, entry.data[CONF_PRIVATE_KEY], host_key)
            except SSHAuthError:
                errors["base"] = "key_rejected"
            except (SSHConnectError, SSHTimeout):
                errors["base"] = "cannot_connect"
            except SSHHostKeyError:
                errors["base"] = "host_key_changed"
            except SSHKeyError:
                errors["base"] = "key_unreadable"
            except NotUnraid:
                errors["base"] = "not_unraid"
            else:
                return self.async_update_reload_and_abort(entry, data_updates={CONF_HOST_KEY: host_key})

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Optional(CONF_ACCEPT_HOST_KEY, default=False): BooleanSelector()}),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Change host/port; the key pair stays."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            host = str(user_input[CONF_HOST]).strip()
            port = int(user_input[CONF_PORT])
            # The unique id IS the address, so changing the address changes it.
            # Check before dialling: an address another entry already owns must
            # not cost two SSH connections first.
            new_unique_id = f"{host}:{port}"
            if new_unique_id != entry.unique_id:
                await self.async_set_unique_id(new_unique_id)
                self._abort_if_unique_id_configured()
            try:
                host_key = await fetch_host_key(host, port)
                await validate(host, port, entry.data[CONF_PRIVATE_KEY], host_key)
            except SSHAuthError:
                errors["base"] = "key_rejected"
            except (SSHConnectError, SSHTimeout):
                errors["base"] = "cannot_connect"
            except SSHHostKeyError:
                errors["base"] = "host_key_changed"
            except SSHKeyError:
                errors["base"] = "key_unreadable"
            except NotUnraid:
                errors["base"] = "not_unraid"
            else:
                return self.async_update_reload_and_abort(
                    entry,
                    unique_id=new_unique_id,
                    data_updates={CONF_HOST: host, CONF_PORT: port, CONF_HOST_KEY: host_key},
                )
        schema = vol.Schema(
            {
                vol.Required(CONF_HOST, default=entry.data[CONF_HOST]): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.TEXT)
                ),
                vol.Required(CONF_PORT, default=entry.data.get(CONF_PORT, DEFAULT_PORT)): NumberSelector(
                    NumberSelectorConfig(min=1, max=65535, mode=NumberSelectorMode.BOX)
                ),
            }
        )
        return self.async_show_form(step_id="reconfigure", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> "UnraidOptionsFlow":
        return UnraidOptionsFlow()


class UnraidOptionsFlow(OptionsFlow):
    """Polling interval and update-check interval."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self.config_entry
        if user_input is not None:
            return self.async_create_entry(
                data={
                    CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL]),
                    CONF_UPDATE_INTERVAL: int(user_input[CONF_UPDATE_INTERVAL]),
                }
            )
        scan = entry.options.get(CONF_SCAN_INTERVAL, entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
        upd = entry.options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)
        schema = vol.Schema(
            {
                vol.Required(CONF_SCAN_INTERVAL, default=scan): NumberSelector(
                    NumberSelectorConfig(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL, step=5,
                                         mode=NumberSelectorMode.BOX, unit_of_measurement="s")
                ),
                vol.Required(CONF_UPDATE_INTERVAL, default=upd): NumberSelector(
                    NumberSelectorConfig(min=MIN_UPDATE_INTERVAL, max=MAX_UPDATE_INTERVAL, step=1,
                                         mode=NumberSelectorMode.BOX, unit_of_measurement="h")
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
