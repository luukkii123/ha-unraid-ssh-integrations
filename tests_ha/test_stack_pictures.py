"""Stack pictures in the cache and on the stack switch; plain-http labels fetched by the server."""
from __future__ import annotations

from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from custom_components.unraid_ssh.icon_cache import STACK_PREFIX
from custom_components.unraid_ssh.icons import ICON_FETCH_HTTP_ERROR, IconSource
from custom_components.unraid_ssh.model import Snapshot, Stack
from custom_components.unraid_ssh.parse import Container, StackServices
from custom_components.unraid_ssh.ssh import CommandResult
from test_container_pictures import setup_entry
from test_icon_cache import PNG, PictureSSH, cache_class

FOLDER = '/boot/config/plugins/compose.manager/projects/Webstack/'


def stack_snapshot(*, members=True, sources=True, web_label=None, services=None, extra_stacks=()):
    web = Container('webstack-web-1', 'running', 'example/web', 'webstack', 'web', web_label)
    db = Container('webstack-db-1', 'running', 'example/db', 'webstack', 'db')
    containers = (db, web) if members else ()
    if services is None:
        services = StackServices(('web', 'db'), {'web': 'https://example.com/web.png'})
    stack = Stack('webstack', 'webstack', FOLDER, True, True, len(containers), (FOLDER + 'docker-compose.yml',),
                  containers, display_name='Web Stack', services=services)
    icon_sources = {'webstack-db-1': IconSource('file', '/var/lib/docker/unraid/images/db.png', '1:68')} if sources else {}
    return Snapshot(None, (), (), None, None, None, None, (), (stack, *extra_stacks), (), containers, (), frozenset(), icon_sources)


class FetchSSH(PictureSSH):
    """Answers reads and fetches; remembers which kind of command it saw."""

    def __init__(self):
        super().__init__()
        self.commands = []
        self.fetch_status = 0

    async def run(self, command, *, timeout):
        self.commands.append('fetch' if 'curl' in command else 'read')
        if 'curl' in command and self.fetch_status:
            self.calls += 1
            return CommandResult(self.fetch_status, '', '')
        return await super().run(command, timeout=timeout)


async def test_stack_picture_uses_first_pictured_service_in_compose_order(hass, tmp_path):
    hass.config.config_dir = str(tmp_path)
    client = FetchSSH()
    cache = cache_class()(hass, 'entry-one', client)
    # web has only an https label, db a cache file: web comes first in compose order.
    cache.schedule(stack_snapshot())
    await hass.async_block_till_done()
    assert cache.picture(STACK_PREFIX + 'Webstack') == 'https://example.com/web.png'
    assert cache.picture('webstack-db-1').startswith('/local/unraid_ssh/entry-one/')
    # Order db first: the stack mirrors db's cached file.
    cache.schedule(stack_snapshot(services=StackServices(('db', 'web'), {'web': 'https://example.com/web.png'})))
    await hass.async_block_till_done()
    assert cache.picture(STACK_PREFIX + 'Webstack') == cache.picture('webstack-db-1')
    # Down and without inventory: the compose label still gives the picture.
    cache.schedule(stack_snapshot(members=False, sources=False))
    await hass.async_block_till_done()
    assert cache.picture(STACK_PREFIX + 'Webstack') == 'https://example.com/web.png'
    assert 'fetch' not in client.commands
    # Nothing at all: no picture, the card falls back to its icon.
    cache.schedule(stack_snapshot(members=False, sources=False, services=StackServices(('web',))))
    await hass.async_block_till_done()
    assert cache.picture(STACK_PREFIX + 'Webstack') is None
    await cache.async_shutdown()


async def test_plain_http_label_is_fetched_by_the_server_and_served_locally(hass, tmp_path):
    hass.config.config_dir = str(tmp_path)
    client = FetchSSH()
    cache = cache_class()(hass, 'entry-one', client)
    cache.schedule(stack_snapshot(sources=False, web_label='http://app.example/icon.png'))
    await hass.async_block_till_done()
    local = cache.picture('webstack-web-1')
    assert local.startswith('/local/unraid_ssh/entry-one/') and local.endswith('.png')
    assert (tmp_path / 'www' / local.removeprefix('/local/')).read_bytes() == PNG
    assert cache.picture(STACK_PREFIX + 'Webstack') == local
    assert client.commands.count('fetch') == 2  # container and stack, same content-hashed file
    await cache.async_shutdown()


async def test_http_error_is_final_but_transport_failure_retries(hass, tmp_path):
    hass.config.config_dir = str(tmp_path)
    client = FetchSSH()
    client.fetch_status = ICON_FETCH_HTTP_ERROR
    cache = cache_class()(hass, 'entry-one', client)
    snap = stack_snapshot(members=False, sources=False, services=StackServices(('web',), {'web': 'http://app.example/missing.png'}))
    cache.schedule(snap)
    await hass.async_block_till_done()
    # The browser may still reach a LAN host the server could not: keep the URL.
    assert cache.picture(STACK_PREFIX + 'Webstack') == 'http://app.example/missing.png'
    cache.schedule(snap)
    await hass.async_block_till_done()
    assert client.commands.count('fetch') == 1
    client.fetch_status = 6  # curl: could not resolve host -- transient
    cache.schedule(stack_snapshot(members=False, sources=False, services=StackServices(('web',), {'web': 'http://app.example/other.png'})))
    await hass.async_block_till_done()
    # Transport failure keeps the last published picture and retries next poll.
    assert cache.picture(STACK_PREFIX + 'Webstack') == 'http://app.example/missing.png'
    cache.schedule(stack_snapshot(members=False, sources=False, services=StackServices(('web',), {'web': 'http://app.example/other.png'})))
    await hass.async_block_till_done()
    assert client.commands.count('fetch') == 3
    await cache.async_shutdown()


async def test_http_fetch_disabled_without_local_route(hass, tmp_path):
    hass.config.config_dir = str(tmp_path)
    client = FetchSSH()
    cache = cache_class()(hass, 'entry-one', client, local_enabled=False)
    cache.schedule(stack_snapshot(sources=False, web_label='http://app.example/icon.png'))
    await hass.async_block_till_done()
    assert cache.picture('webstack-web-1') == 'http://app.example/icon.png'
    assert client.commands == []
    await cache.async_shutdown()


async def test_stack_switch_shows_picture_and_display_name(hass, tmp_path, monkeypatch):
    hass.config.config_dir = str(tmp_path)
    assert await async_setup_component(hass, 'http', {})
    client = FetchSSH()
    entry = await setup_entry(hass, monkeypatch, client, stack_snapshot())
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id('switch', 'unraid_ssh', entry.entry_id + '_stack_Webstack')
    state = hass.states.get(entity_id)
    assert state.attributes['entity_picture'] == 'https://example.com/web.png'
    assert state.attributes['stack_display_name'] == 'Web Stack'
    assert state.attributes['stack_name'] == 'webstack'
    web = [s for s in hass.states.async_all('switch') if s.attributes.get('container_name') == 'webstack-web-1']
    assert len(web) == 1 and web[0].attributes['stack_display_name'] == 'Web Stack'
    # A new reading updates the stack picture through the cache listener.
    entry.runtime_data.coordinator.async_set_updated_data(
        stack_snapshot(services=StackServices(('db', 'web'), {'web': 'https://example.com/web.png'})))
    await hass.async_block_till_done()
    picture = hass.states.get(entity_id).attributes['entity_picture']
    assert picture.startswith('/local/unraid_ssh/')
    assert await hass.config_entries.async_unload(entry.entry_id)
