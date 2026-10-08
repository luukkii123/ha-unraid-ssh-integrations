"""Real HA entities keep queued/running state across wrapper calls and reload."""
import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

from custom_components.unraid_ssh.model import ImageStatus
from custom_components.unraid_ssh.ssh import CommandResult
from homeassistant.helpers.entity_platform import async_get_platforms
from test_device_migration import inventory, legacy, load
from test_card_contract import loaded


async def test_native_batch_admits_all_once_and_reload_preserves_jobs(hass, monkeypatch, tmp_path):
    hass.config.config_dir = str(tmp_path)
    entry,_,_=legacy(hass)
    snap=inventory()
    images={c.name:ImageStatus(c.name,c.image,'sha256:old','sha256:new',True) for c in snap.containers}
    await load(hass,monkeypatch,entry,snap,images)
    queue=entry.runtime_data.queue
    # Freeze execution at admission; no SSH job is allowed in this test.
    monkeypatch.setattr(queue,'start',lambda:None)
    batch=next(e for p in async_get_platforms(hass,'unraid_ssh') for e in p.entities.values()
               if e.unique_id==entry.entry_id+'_update_all')
    await asyncio.gather(batch.async_press(),batch.async_press())
    updates=loaded(hass,'update')
    assert len(queue.jobs)==len(snap.containers)
    assert all(e.state_attributes['in_progress'] is True for e in updates)
    assert all(e.extra_state_attributes['update_state']=='queued' for e in updates)
    await updates[0].async_install_with_progress(None,False)
    assert len(queue.jobs)==len(snap.containers)
    await queue.changed()
    ids=[j['id'] for j in queue.jobs]
    # Reload restores the storage records. Synthetic SSH cannot launch a real job.
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    restored=entry.runtime_data.queue
    assert [j['id'] for j in restored.jobs]==ids
    assert all(e.state_attributes['in_progress'] is True for e in loaded(hass,'update'))
    assert restored.summary()['active'] and restored.summary()['total_count']==len(snap.containers)
    await hass.config_entries.async_unload(entry.entry_id)
