"""Synthetic pictures exercise validation, coalescing and file lifecycle."""
from __future__ import annotations

import asyncio
import base64
from dataclasses import replace
from io import BytesIO
from pathlib import Path

from PIL import Image
import pytest

from custom_components.unraid_ssh import icons
from custom_components.unraid_ssh.icons import IconSource
from custom_components.unraid_ssh.model import Snapshot
from custom_components.unraid_ssh.parse import Container
from custom_components.unraid_ssh.ssh import CommandResult, SSHError

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC')
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><path d="M0 0h10v10z" fill="red"/></svg>'


def snapshot(*, revision='1:68', url='', sources=True, names=('web',)):
    containers = tuple(Container(n, 'running', 'example/web:latest', '', '', url) for n in names)
    return Snapshot(None, (), (), None, None, None, None, (), (), containers, containers, (), frozenset(),
                    {n: IconSource('file', '/var/lib/docker/unraid/images/' + n + '.png', revision) for n in names} if sources else {})


class PictureSSH:
    def __init__(self, data=PNG):
        self.data = data
        self.calls = 0
        self.active = 0
        self.maximum = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        self.error = False

    async def run(self, command, *, timeout):
        self.calls += 1
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        self.started.set()
        data = self.data
        try:
            await self.release.wait()
            if self.error:
                raise SSHError('synthetic transport failure')
            return CommandResult(0, base64.b64encode(data).decode(), '')
        finally:
            self.active -= 1


def cache_class():
    from custom_components.unraid_ssh import icon_cache
    return icon_cache.ContainerIconCache


@pytest.mark.parametrize('data,extension', [(PNG, 'png'), (SVG, 'svg')])
def test_valid_images(data, extension):
    assert hasattr(icons, 'validate_icon'), 'image validation missing'
    result = icons.validate_icon(data)
    assert result is not None and result[1] == extension


@pytest.mark.parametrize('data', [b'', b'not an image', PNG[:30], b'x' * 1048577,
    b'<!DOCTYPE svg><svg xmlns="http://www.w3.org/2000/svg"/>',
    SVG.replace(b'<path', b'<script>alert(1)</script><path'),
    SVG.replace(b'<path', b'<foreignObject/><path'),
    SVG.replace(b'<path', b'<path onload="alert(1)"'),
    SVG.replace(b'<path', b'<image href="https://example.com/image.png"/><path'),
    SVG.replace(b'<path', b'<style>@import "https://example.com/a.css";</style><path'),
    SVG.replace(b'fill="red"', b'fill="url(https://example.com/a.svg)"'),
    SVG.replace(b'<path', b'<animate attributeName="href"/><path'),
    b'<?xml-stylesheet href="https://example.com/a.css"?><svg xmlns="http://www.w3.org/2000/svg"/>',
], ids=['empty', 'text', 'truncated', 'oversize', 'doctype', 'script', 'foreign-object', 'event', 'external-image', 'css-import', 'css-url', 'animate', 'stylesheet-pi'])
def test_rejects_invalid_or_active_images(data):
    assert hasattr(icons, 'validate_icon'), 'image validation missing'
    assert icons.validate_icon(data) is None


@pytest.mark.parametrize('format,extension', [('JPEG', 'jpg'), ('GIF', 'gif'), ('WEBP', 'webp')])
def test_other_raster_formats_are_decoded(format, extension):
    stream = BytesIO()
    Image.new('RGB', (2, 2), 'red').save(stream, format=format)
    assert hasattr(icons, 'validate_icon'), 'image validation missing'
    assert icons.validate_icon(stream.getvalue())[1] == extension


async def test_cache_reuses_changes_removes_and_keeps_transport_failure(hass, tmp_path):
    hass.config.config_dir = str(tmp_path)
    client = PictureSSH()
    cache = cache_class()(hass, 'entry-one', client)
    changed = []
    cache.async_add_listener(lambda: changed.append(cache.picture('web')))
    cache.schedule(snapshot())
    await hass.async_block_till_done()
    first = cache.picture('web')
    assert first.startswith('/local/unraid_ssh/entry-one/')
    target = tmp_path / 'www' / first.removeprefix('/local/')
    assert target.read_bytes() == PNG
    cache.schedule(snapshot())
    await hass.async_block_till_done()
    assert client.calls == 1
    client.error = True
    cache.schedule(snapshot(revision='2:68'))
    await hass.async_block_till_done()
    assert cache.picture('web') == first and target.exists()
    client.error = False
    client.data = SVG
    cache.schedule(snapshot(revision='2:68'))
    await hass.async_block_till_done()
    second = cache.picture('web')
    assert second != first and second.endswith('.svg') and not target.exists()
    cache.schedule(replace(snapshot(sources=False), icons_valid=False))
    cache.schedule(replace(snapshot(sources=False), failed=frozenset({'docker'})))
    await hass.async_block_till_done()
    assert cache.picture('web') == second
    cache.schedule(snapshot(sources=False))
    await hass.async_block_till_done()
    assert cache.picture('web') is None
    assert len(changed) == 3
    assert list((tmp_path / 'www/unraid_ssh/entry-one').iterdir()) == []
    await cache.async_shutdown()


async def test_invalid_file_falls_back_to_label_then_mdi(hass, tmp_path):
    hass.config.config_dir = str(tmp_path)
    client = PictureSSH(b'not-image')
    cache = cache_class()(hass, 'entry-one', client)
    cache.schedule(snapshot(url='https://example.com/web.png'))
    await hass.async_block_till_done()
    assert cache.picture('web') == 'https://example.com/web.png'
    cache.schedule(snapshot())
    await hass.async_block_till_done()
    assert cache.picture('web') is None
    await cache.async_shutdown()


async def test_coalesces_bounded_work_and_ignores_stale_completion(hass, tmp_path):
    hass.config.config_dir = str(tmp_path)
    client = PictureSSH()
    client.release.clear()
    cache = cache_class()(hass, 'entry-one', client)
    cache.schedule(snapshot(names=('web', 'other', 'third')))
    await client.started.wait()
    cache.schedule(snapshot(sources=False, url='https://example.com/new.png'))
    client.release.set()
    await hass.async_block_till_done()
    assert cache.picture('web') == 'https://example.com/new.png'
    assert cache.picture('other') is None
    assert client.calls <= 2 and client.maximum <= 2
    assert not list((tmp_path / 'www/unraid_ssh/entry-one').glob('*.*'))
    await cache.async_shutdown()


async def test_entries_isolated_and_shutdown_cancels_active_read(hass, tmp_path):
    hass.config.config_dir = str(tmp_path)
    first_client, second_client = PictureSSH(), PictureSSH()
    first = cache_class()(hass, 'entry-one', first_client)
    second = cache_class()(hass, 'entry-two', second_client)
    first.schedule(snapshot())
    second.schedule(snapshot())
    await hass.async_block_till_done()
    assert first.picture('web') != second.picture('web')
    first_client.release.clear()
    first_client.started.clear()
    first.schedule(snapshot(revision='2:68'))
    await first_client.started.wait()
    await first.async_shutdown()
    assert first_client.active == 0
    first.schedule(snapshot(revision='3:68'))
    await hass.async_block_till_done()
    assert first_client.calls == 2
    assert second.picture('web') is not None
    await second.async_shutdown()


async def test_schedule_during_cleanup_is_not_lost(hass, tmp_path, monkeypatch):
    import threading
    hass.config.config_dir = str(tmp_path)
    client = PictureSSH()
    cache = cache_class()(hass, 'entry-one', client)
    started = asyncio.Event()
    release = threading.Event()
    original = cache._cleanup

    def cleanup(keep):
        hass.loop.call_soon_threadsafe(started.set)
        release.wait(3)
        original(keep)

    monkeypatch.setattr(cache, '_cleanup', cleanup)
    cache.schedule(snapshot(names=()))
    await started.wait()
    cache.schedule(snapshot())
    release.set()
    await hass.async_block_till_done()
    assert cache.picture('web') is not None
    await cache.async_shutdown()


async def test_startup_only_cleans_own_stale_files_and_rejects_symlink_directory(hass, tmp_path):
    hass.config.config_dir = str(tmp_path)
    directory = tmp_path / 'www/unraid_ssh/entry-one'
    directory.mkdir(parents=True)
    old = directory / ('a' * 64 + '.png')
    old.write_bytes(PNG)
    unrelated = directory / 'user-file.txt'
    unrelated.write_text('keep')
    cache = cache_class()(hass, 'entry-one', PictureSSH())
    cache.schedule(snapshot(names=()))
    await hass.async_block_till_done()
    assert not old.exists() and unrelated.read_text() == 'keep'
    await cache.async_shutdown()
    redirected = tmp_path / 'www/unraid_ssh/entry-two'
    redirected.symlink_to(directory, target_is_directory=True)
    second = cache_class()(hass, 'entry-two', PictureSSH())
    second.schedule(snapshot())
    await hass.async_block_till_done()
    assert second.picture('web') is None
    assert list(directory.iterdir()) == [unrelated]
    await second.async_shutdown()


async def test_oversize_base64_never_creates_a_file(hass, tmp_path):
    hass.config.config_dir = str(tmp_path)
    cache = cache_class()(hass, 'entry-one', PictureSSH(b'x' * 1048577))
    cache.schedule(snapshot())
    await hass.async_block_till_done()
    assert cache.picture('web') is None
    assert not list((tmp_path / 'www/unraid_ssh/entry-one').iterdir())
    await cache.async_shutdown()


def test_pixel_budget_rejects_small_compressed_bomb():
    stream = BytesIO()
    Image.new('RGB', (2049, 2048)).save(stream, format='PNG')
    assert len(stream.getvalue()) < 1048576
    assert icons.validate_icon(stream.getvalue()) is None


async def test_new_file_revision_wins_over_running_old_read(hass, tmp_path):
    hass.config.config_dir = str(tmp_path)
    client = PictureSSH()
    client.release.clear()
    cache = cache_class()(hass, 'entry-one', client)
    cache.schedule(snapshot())
    await client.started.wait()
    client.data = SVG
    cache.schedule(snapshot(revision='2:100'))
    client.release.set()
    await hass.async_block_till_done()
    assert client.calls == 2
    picture = cache.picture('web')
    assert picture.endswith('.svg')
    files = list((tmp_path / 'www/unraid_ssh/entry-one').iterdir())
    assert len(files) == 1 and files[0].suffix == '.svg'
    await cache.async_shutdown()


async def test_shutdown_waits_for_executor_write_before_entry_removal(hass, tmp_path, monkeypatch):
    import threading
    from custom_components.unraid_ssh.icon_cache import async_remove_icon_files
    hass.config.config_dir = str(tmp_path)
    client = PictureSSH()
    cache = cache_class()(hass, 'entry-one', client)
    started = asyncio.Event()
    release = threading.Event()
    original = cache._write

    def write(data, extension):
        hass.loop.call_soon_threadsafe(started.set)
        release.wait(3)
        return original(data, extension)

    monkeypatch.setattr(cache, '_write', write)
    cache.schedule(snapshot())
    await started.wait()
    shutdown = asyncio.create_task(cache.async_shutdown())
    await asyncio.sleep(0)
    assert not shutdown.done()
    release.set()
    await shutdown
    await async_remove_icon_files(hass, 'entry-one')
    await hass.async_block_till_done()
    assert cache.picture('web') is None
    assert not (tmp_path / 'www/unraid_ssh/entry-one').exists()


def nested_svg(depth):
    return b'<svg xmlns="http://www.w3.org/2000/svg">' + b'<g>' * depth + b'<path d="M0 0h1v1z"/>' + b'</g>' * depth + b'</svg>'


@pytest.mark.parametrize('depth', [65, 1500])
def test_rejects_excessive_svg_nesting_without_serializer_exception(depth):
    assert icons.validate_icon(nested_svg(depth)) is None


def test_accepts_ordinary_svg_group_nesting():
    assert icons.validate_icon(nested_svg(8))[1] == 'svg'


@pytest.mark.parametrize('url', ['', 'https://example.com/web.png'])
async def test_deep_svg_uses_url_or_mdi_fallback_and_finishes_revision(hass, tmp_path, url):
    hass.config.config_dir = str(tmp_path)
    client = PictureSSH()
    cache = cache_class()(hass, 'entry-one', client)
    cache.schedule(snapshot())
    await hass.async_block_till_done()
    assert cache.picture('web').endswith('.png')
    client.data = nested_svg(1500)
    cache.schedule(snapshot(revision='2:10569', url=url))
    await hass.async_block_till_done()
    assert cache.picture('web') == (url or None)
    cache.schedule(snapshot(revision='2:10569', url=url))
    await hass.async_block_till_done()
    assert client.calls == 2
    assert list((tmp_path / 'www/unraid_ssh/entry-one').iterdir()) == []
    await cache.async_shutdown()
