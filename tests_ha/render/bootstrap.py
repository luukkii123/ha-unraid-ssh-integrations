"""Run actual HA with synthetic platforms; all SSH transport is fail-closed.

Run only in the documented disposable container. Runtime files go to /ui.
"""
import asyncio
import base64
from dataclasses import replace
from datetime import timedelta
import json
import logging
import os
import signal
import socket
from pathlib import Path
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, '/repo')

from homeassistant import bootstrap, loader
from homeassistant.auth.const import GROUP_ID_ADMIN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er, device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry
from custom_components.unraid_ssh.coordinator import UpdateState
from custom_components.unraid_ssh.icons import IconSource, build_icon_read_command
from custom_components.unraid_ssh.model import ImageStatus
from custom_components.unraid_ssh.ssh import CommandResult
from test_device_migration import inventory

ICON = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="12" fill="#007f88"/><path d="M12 16h40v10H12zm0 22h40v10H12z" fill="white"/></svg>'
ICON_PATH = '/var/lib/docker/unraid/images/alpha_one.png'

class FakeSSH:
    async def run(self, command, *, timeout):
        if command != build_icon_read_command(ICON_PATH):
            raise AssertionError('Synthetic fixture rejects non-picture commands')
        return CommandResult(0, base64.b64encode(ICON).decode(), '')

async def deny_ssh(*args, **kwargs):
    raise AssertionError('Real SSH is forbidden in synthetic acceptance')

async def main():
    os.umask(0o077)
    # Refuse a busy test port instead of allowing HA to fall back to another.
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(('127.0.0.1', 18123))
    root = Path('/ui')
    config_dir = root / 'config'
    config_dir.mkdir(exist_ok=True)
    (config_dir / 'custom_components').symlink_to('/repo/custom_components', target_is_directory=True)
    (config_dir / '.storage').mkdir(exist_ok=True)
    (config_dir / '.storage/onboarding').write_text(json.dumps({'version': 4, 'minor_version': 1, 'key': 'onboarding', 'data': {'done': ['user', 'core_config', 'integration', 'analytics']}}))
    (config_dir / 'www').mkdir(exist_ok=True)
    (config_dir / 'www/busch-cards.js').write_bytes(Path('/cards/busch-cards.js').read_bytes())
    language = os.environ.get('UI_LANGUAGE', 'de')
    hass = HomeAssistant(str(config_dir))
    loader.async_setup(hass)
    config = {
        'homeassistant': {'name': 'Example Unraid', 'latitude': 0, 'longitude': 0, 'elevation': 0, 'unit_system': 'metric', 'time_zone': 'Europe/Berlin', 'country': 'DE', 'language': language},
        'http': {'server_host': '127.0.0.1', 'server_port': 18123},
        'api': {}, 'websocket_api': {}, 'frontend': {}, 'config': {},
        'lovelace': {'mode': 'yaml', 'resources': [{'url': '/local/busch-cards.js', 'type': 'module'}]},
    }
    assert await bootstrap.async_from_config_dict(config, hass)
    state = inventory()
    # alpha: SSH-copied picture. beta: URL fallback, browser can deny its origin.
    containers = (state.containers[0], replace(state.containers[1], icon='http://images.example.invalid/fallback.svg'))
    state = replace(state, containers=containers, template_containers=containers,
        icon_sources={'alpha_one': IconSource('file', ICON_PATH, 'synthetic:1')})
    images = {c.name: ImageStatus(c.name, c.image, 'sha256:old', 'sha256:new', True) for c in containers}
    with patch('asyncssh.connect', deny_ssh), patch('custom_components.unraid_ssh.ssh.UnraidSSH.run', deny_ssh), \
         patch('custom_components.unraid_ssh.build_client', lambda entry: FakeSSH()), \
         patch('custom_components.unraid_ssh.coordinator.UnraidCoordinator._async_update_data', AsyncMock(return_value=state)), \
         patch('custom_components.unraid_ssh.coordinator.UpdateCoordinator._async_update_data', AsyncMock(return_value=UpdateState(images, None))):
        entry = MockConfigEntry(domain='unraid_ssh', title='Example Unraid', data={'host': 'unraid.example.invalid'})
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        registry = er.async_get(hass)
        entities = {e.unique_id.removeprefix(entry.entry_id): e.entity_id for e in er.async_entries_for_config_entry(registry, entry.entry_id)}
        switch = entities['_container_alpha_one']
        update = entities['_update_alpha_one']
        cards = [
            {'type': 'entities', 'title': 'Container pictures', 'entities': [switch, update, entities['_container_beta'], entities['_update_beta']]},
            {'type': 'tile', 'entity': switch, 'show_entity_picture': True},
            {'type': 'tile', 'entity': update, 'show_entity_picture': True},
            {'type': 'custom:busch-device-card', 'entity': switch},
            {'type': 'entities', 'title': 'GPU / Shares', 'entities': [entities[k] for k in entities if k.startswith(('_gpu_', '_share_'))]},
        ]
        (config_dir / 'ui-lovelace.yaml').write_text(json.dumps({'title': 'Synthetic acceptance', 'views': [{'title': 'Devices and pictures', 'path': 'acceptance', 'panel': True, 'cards': [{'type': 'vertical-stack', 'cards': cards}]}]}))
        devices = [{'id': d.id, 'name': d.name} for d in dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)]
        (root / 'inventory.json').write_text(json.dumps({'entities': entities, 'devices': devices, 'language': language}))
        user = await hass.auth.async_create_user('Visual Test', group_ids=[GROUP_ID_ADMIN])
        refresh = await hass.auth.async_create_refresh_token(user, client_id='http://127.0.0.1:18123/', client_name='Synthetic UI verification', token_type='long_lived_access_token', access_token_expiration=timedelta(days=1))
        token = hass.auth.async_create_access_token(refresh)
        (root / 'auth.json').write_text(json.dumps({'access_token': token, 'token_type': 'Bearer', 'expires_in': 86400, 'hassUrl': 'http://127.0.0.1:18123', 'clientId': 'http://127.0.0.1:18123/', 'expires': 9999999999999}))
        await hass.async_start()
        if hass.http.server_port != 18123:
            await hass.async_stop()
            raise RuntimeError('Synthetic HA refused an unexpected HTTP port')
        print('SYNTHETIC_HA_READY', flush=True)
        stop = asyncio.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            asyncio.get_running_loop().add_signal_handler(sig, stop.set)
        try:
            await stop.wait()
        finally:
            await hass.async_stop()

if __name__ == '__main__':
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(main())
