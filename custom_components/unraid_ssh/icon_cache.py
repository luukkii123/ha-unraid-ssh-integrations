"""Entry-scoped, bounded copies of existing Unraid container pictures."""
from __future__ import annotations

import asyncio
import base64
import binascii
from collections.abc import Callable
from functools import partial
import hashlib
import logging
from pathlib import Path
import re
import shutil
import tempfile

from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant, callback

from .const import POLL_TIMEOUT
from .icons import IconSource, MAX_ICON_BYTES, build_icon_read_command, select_icon, validate_icon
from .model import Snapshot
from .ssh import SSHError, UnraidSSH

_LOGGER = logging.getLogger(__name__)
_HASH_FILE = re.compile(r'[0-9a-f]{64}\.(?:png|jpg|gif|webp|svg)')
_HTTP_KEY = 'unraid_ssh_icon_http'


def _directory(hass: HomeAssistant, entry_id: str) -> Path:
    if not re.fullmatch(r'[A-Za-z0-9_-]+', entry_id):
        raise ValueError('invalid icon cache entry id')
    return Path(hass.config.path('www', 'unraid_ssh', entry_id))


def _prepare(path: Path, *, entry: bool = True) -> None:
    # Refuse redirects in our own tree, including www. Config dir may itself be
    # a host-managed symlink; never follow an entry directory elsewhere.
    directories = (path.parent.parent, path.parent, path) if entry else (path.parent, path)
    for directory in directories:
        if directory.is_symlink():
            raise OSError('icon cache directory is a symlink')
        directory.mkdir(exist_ok=True)


async def async_setup_icon_http(hass: HomeAssistant) -> None:
    """Register once even when frontend started before www existed."""
    async def setup() -> None:
        root = Path(hass.config.path('www', 'unraid_ssh'))
        await hass.async_add_executor_job(partial(_prepare, root, entry=False))
        await hass.http.async_register_static_paths([
            StaticPathConfig('/local/unraid_ssh', str(root), True),
        ])

    if _HTTP_KEY not in hass.data:
        hass.data[_HTTP_KEY] = hass.async_create_task(setup())
    try:
        await asyncio.shield(hass.data[_HTTP_KEY])
    except Exception:
        hass.data.pop(_HTTP_KEY, None)
        raise


async def async_remove_icon_files(hass: HomeAssistant, entry_id: str) -> None:
    path = _directory(hass, entry_id)

    def remove() -> None:
        if path.parent.is_symlink() or path.parent.parent.is_symlink() or path.is_symlink():
            raise OSError('icon cache directory is a symlink')
        if path.exists():
            shutil.rmtree(path)

    await hass.async_add_executor_job(remove)


class ContainerIconCache:
    """Two workers drain a coalesced desired-source map; properties stay in RAM."""

    def __init__(self, hass: HomeAssistant, entry_id: str, client: UnraidSSH) -> None:
        self._hass = hass
        self._client = client
        self._directory = _directory(hass, entry_id)
        self._prefix = f'/local/unraid_ssh/{entry_id}/'
        self._desired: dict[str, tuple[IconSource | None, str | None]] = {}
        self._pending: dict[str, tuple[IconSource | None, str | None]] = {}
        self._finished: dict[str, tuple[IconSource | None, str | None]] = {}
        self._inflight: dict[str, tuple[IconSource | None, str | None]] = {}
        self._pictures: dict[str, str] = {}
        self._listeners: set[Callable[[], None]] = set()
        self._workers: set[asyncio.Task] = set()
        self._files_lock = asyncio.Lock()
        self._closed = False
        self._started = False

    @callback
    def schedule(self, snapshot: Snapshot) -> None:
        if self._closed or not snapshot.icons_valid or 'docker' in snapshot.failed:
            return
        self._desired = {}
        for container in snapshot.containers:
            fallback = select_icon(container, {})
            self._desired[container.name] = (
                select_icon(container, snapshot.icon_sources),
                fallback.value if fallback else None,
            )
        for name in self._pictures.keys() | self._finished.keys() | self._inflight.keys():
            if name not in self._desired:
                self._desired[name] = (None, None)
        self._pending = {
            name: source for name, source in self._desired.items()
            if self._finished.get(name) != source and self._inflight.get(name) != source
        }
        if self._pending or not self._started:
            self._start_workers()

    def _start_workers(self) -> None:
        self._started = True
        while len(self._workers) < 2 and not self._closed:
            task = self._hass.async_create_task(self._work(), 'unraid_ssh container pictures')
            self._workers.add(task)
            task.add_done_callback(self._worker_done)

    @callback
    def _worker_done(self, task: asyncio.Task) -> None:
        self._workers.discard(task)
        # A new snapshot may have arrived while both workers were cleaning up.
        if any(name not in self._inflight for name in self._pending) and not self._closed:
            self._start_workers()

    @callback
    def picture(self, name: str) -> str | None:
        return self._pictures.get(name)

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.add(listener)
        return lambda: self._listeners.discard(listener)

    def _publish(self, name: str, value: str | None) -> None:
        previous = self._pictures.get(name)
        if value is None:
            self._pictures.pop(name, None)
        else:
            self._pictures[name] = value
        if value != previous:
            for listener in tuple(self._listeners):
                listener()

    async def _executor(self, function, *args):
        # Cancelling an asyncio task cannot stop an executor write. Wait for it
        # before allowing unload/removal to delete the entry's directory.
        future = self._hass.async_add_executor_job(function, *args)
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            await future
            raise

    def _write(self, image: bytes, extension: str) -> str:
        _prepare(self._directory)
        filename = f'{hashlib.sha256(image).hexdigest()}.{extension}'
        target = self._directory / filename
        # Replace even an existing target: never trust leftovers as image data.
        with tempfile.NamedTemporaryFile(dir=self._directory, prefix='.icon-', delete=False) as stream:
            temporary = Path(stream.name)
            try:
                stream.write(image)
                stream.flush()
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
        return self._prefix + filename

    def _cleanup(self, keep: set[str]) -> None:
        _prepare(self._directory)
        for path in self._directory.iterdir():
            if (path.name not in keep and
                    (_HASH_FILE.fullmatch(path.name) or path.name.startswith('.icon-'))
                    and not path.is_dir()):
                path.unlink(missing_ok=True)

    async def _work(self) -> None:
        try:
            while self._pending and not self._closed:
                name = next((name for name in self._pending if name not in self._inflight), None)
                if name is None:
                    break
                selected = self._pending.pop(name)
                self._inflight[name] = selected
                source, fallback = selected
                try:
                    picture = fallback
                    image = None
                    if source and source.kind == 'file':
                        result = await self._client.run(build_icon_read_command(source.value), timeout=POLL_TIMEOUT)
                        if result.exit_status:
                            raise SSHError('icon read failed')
                        if len(result.stdout) <= 4 * ((MAX_ICON_BYTES + 2) // 3):
                            try:
                                data = base64.b64decode(result.stdout, validate=True)
                                image = await self._executor(validate_icon, data)
                            except (ValueError, binascii.Error):
                                pass
                        if image is None:
                            _LOGGER.debug('Container icon rejected by image validation')
                    if self._desired.get(name) != selected or self._closed:
                        continue
                    async with self._files_lock:
                        if image:
                            picture = await self._executor(self._write, *image)
                        if self._desired.get(name) == selected and not self._closed:
                            self._finished[name] = selected
                            self._publish(name, picture)
                except (SSHError, OSError):
                    # No path, image content, stdout or server credentials in logs.
                    _LOGGER.debug('Container icon read or local cache write failed; keeping previous picture')
                finally:
                    self._inflight.pop(name, None)
            async with self._files_lock:
                keep = {url.removeprefix(self._prefix) for url in self._pictures.values() if url.startswith(self._prefix)}
                await self._executor(self._cleanup, keep)
        except OSError:
            _LOGGER.debug('Container icon cache cleanup failed')

    async def async_shutdown(self) -> None:
        self._closed = True
        self._listeners.clear()
        self._pending.clear()
        for task in self._workers:
            task.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
