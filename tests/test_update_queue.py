"""Queue contracts with synthetic SSH, storage and real shell admission."""
import asyncio
import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from unraid_ssh import update_queue as q
from unraid_ssh.model import Snapshot, ImageStatus
from unraid_ssh.parse import Container
from unraid_ssh.ssh import CommandResult


class Store:
    data = None
    async def async_load(self):
        return copy.deepcopy(self.data)
    async def async_save(self, data):
        self.data = copy.deepcopy(data)


def fixture(count=3, failed=None):
    containers = [Container(f'c{i}', 'running', 'example/app', None, None) for i in range(count)]
    client = SimpleNamespace(run=AsyncMock(return_value=CommandResult(0, 'verifying', '')))
    fast = SimpleNamespace(data=SimpleNamespace(containers=tuple(containers),stacks=(),failed=frozenset()), client=client,
                           last_update_success=True, async_request_refresh=AsyncMock())
    updates = SimpleNamespace(last_update_success=True, async_request_refresh=AsyncMock(),
        data=SimpleNamespace(images={c.name: ImageStatus(c.name,'example/app','sha256:a','sha256:a',False) for c in containers}))
    hass = SimpleNamespace(async_create_background_task=lambda coro, name: asyncio.create_task(coro))
    queue = q.UpdateQueue(hass, None, fast, updates, Store())
    queue.start = lambda: None
    return queue, containers


@pytest.mark.parametrize('count', [1, 2, 5])
def test_atomic_batch_positions_and_serial_completion(count):
    async def scenario():
        queue, containers = fixture(count)
        await queue.enqueue(containers)
        assert queue.summary()['queued_count'] == count
        assert [queue.attributes(j['key'])['queue_position'] for j in queue.jobs] == list(range(1, count+1))
        assert all(queue.attributes(j['key'])['progress_percent'] is None for j in queue.jobs)
        await queue.run()
        assert queue.summary() == {'active': False, 'current_target': None,'queued_count':0,
            'completed_count':count,'failed_count':0,'total_count':count,'progress_percent':100.0,'blocked':False}
        launches = [call.args[0] for call in queue.fast.client.run.call_args_list if 'nohup' in call.args[0]]
        assert len(launches) == count
        assert all(j['started_at'] and j['finished_at'] for j in queue.jobs)
    asyncio.run(scenario())


def test_failure_in_middle_does_not_drop_the_tail():
    async def scenario():
        queue, containers = fixture()
        await queue.enqueue(containers)
        async def run(command, **kwargs):
            job = next((j for j in queue.jobs if q.job_path(j['id']) in command), None)
            return CommandResult(0, 'failed:7' if job and job['name']=='c1' else 'verifying', '')
        queue.fast.client.run.side_effect = run
        await queue.run()
        assert [j['state'] for j in queue.jobs] == ['completed','failed','completed']
        assert queue.summary()['failed_count'] == 1
        assert queue.summary()['progress_percent'] == 100
        assert 'Weitere Aufträge' in queue.jobs[1]['last_error']
    asyncio.run(scenario())


def test_concurrent_duplicate_all_and_single_are_idempotent():
    async def scenario():
        queue, containers = fixture()
        await asyncio.gather(queue.enqueue(containers), queue.enqueue(containers), queue.enqueue([containers[1]]))
        assert len(queue.jobs)==3 and len(queue.store.data['jobs'])==3
        queue.jobs[0]['state']='pulling'
        await queue.enqueue(containers)
        assert len(queue.jobs)==3
        assert queue.attributes(queue.jobs[0]['key'])['update_state']=='pulling'
        assert queue.summary()['current_target']=='c0'
    asyncio.run(scenario())


def test_reload_observes_existing_job_without_another_launch():
    async def scenario():
        original, containers = fixture(2)
        await original.enqueue(containers)
        original.jobs[0].update(state='recreating', started_at=q.now())
        await original.changed()
        restored, _ = fixture(2)
        restored.store=original.store
        await restored.load()
        assert restored.summary()['active']
        await restored.run()
        launches=[c for c in restored.fast.client.run.call_args_list if 'nohup' in c.args[0]]
        assert len(launches)==1 and restored.jobs[1]['id'] in launches[0].args[0]
    asyncio.run(scenario())


def test_connection_loss_and_timeout_keep_active_target_and_queued_tail(monkeypatch):
    async def actual():
        queue, containers = fixture()
        await queue.enqueue(containers)
        queue.jobs[0].update(state='pulling',started_at=(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat())
        queue.fast.client.run.return_value=CommandResult(0,'pulling','')
        class Stop(Exception): pass
        async def stop(seconds): raise Stop()
        monkeypatch.setattr(q.asyncio,'sleep',stop)
        with pytest.raises(Stop): await queue.run()
        assert queue.jobs[0]['state']=='pulling' and queue.summary()['blocked']
        assert queue.summary()['queued_count']==2
        queue.fast.client.run.side_effect=RuntimeError('private transport text')
        with pytest.raises(Stop): await queue.run()
        assert 'private transport text' not in queue.jobs[0]['last_error']
        assert queue.jobs[0]['finished_at'] is None
    asyncio.run(actual())


def test_store_failure_runs_no_backend_command():
    async def scenario():
        queue, containers = fixture()
        queue.store.async_save=AsyncMock(side_effect=OSError('disk full'))
        with pytest.raises(OSError): await queue.enqueue(containers)
        assert queue.jobs==[]
        queue.fast.client.run.assert_not_awaited()
    asyncio.run(scenario())


def test_real_shell_remote_admission_is_idempotent_and_quoted(tmp_path, monkeypatch):
    import time
    count=tmp_path/'calls'
    root=tmp_path/'jobs'
    root.mkdir()
    monkeypatch.setattr(q,'job_path',lambda job: str(root/job))
    job={'id':'safe','compose':False,'command':f"printf x >> {q.shlex.quote(str(count))}"}
    command=q.launch_command(job)
    for _ in range(2): subprocess.run(['sh','-c',command],check=True,capture_output=True)
    for _ in range(100):
        if (root/'safe'/'status').read_text().strip()=='verifying': break
        time.sleep(.01)
    assert count.read_text()=='x'
    assert (root/'safe'/'status').read_text().strip()=='verifying'


def test_invalid_remote_identity_is_never_a_shell_path():
    with pytest.raises(ValueError): q.job_path('../../arbitrary')


def test_repeated_batch_does_not_requeue_terminal_members_while_tail_runs():
    async def scenario():
        queue, containers = fixture()
        await queue.enqueue(containers)
        queue.jobs[0].update(state="failed",finished_at=q.now())
        queue.jobs[1].update(state="pulling",started_at=q.now())
        await queue.enqueue(containers)
        assert len(queue.jobs)==3 and queue.jobs[0]["state"]=="failed"
    asyncio.run(scenario())


def test_compose_phase_builder_does_not_split_quoted_path_contents(tmp_path,monkeypatch):
    import time
    from unraid_ssh.model import Stack
    folder=tmp_path/'folder && docker compose odd'
    folder.mkdir()
    (folder/'.env').write_text('DEMO=yes\n')
    config=folder/'compose && docker compose file.yaml'
    config.write_text('fixture')
    calls=tmp_path/'calls.jsonl'
    docker=tmp_path/'docker'
    # argv is captured exactly, including paths with shell-looking delimiters.
    docker.write_text('#!/usr/bin/env python3\nimport sys,json\nwith open('+repr(str(calls))+',"a") as f:f.write(json.dumps(sys.argv[1:])+"\\n")\n')
    docker.chmod(0o755)
    c=Container('web','running','example/app','project','svc && strange',replica=1)
    stack=Stack('project','project',str(folder),False,True,1,(str(config),),(c,))
    pull,recreate=q.actions.container_update_phases(c,stack)
    import os,json
    env={**os.environ,'PATH':str(tmp_path)+':'+os.environ['PATH']}
    for cmd in [pull,recreate]:subprocess.run(['sh','-c',cmd],env=env,check=True,capture_output=True)
    recorded=[json.loads(line) for line in calls.read_text().splitlines()]
    assert recorded==[['compose','--env-file',str(folder/'.env'),'-p','project','-f',str(config),'pull','svc && strange'],
                      ['compose','--env-file',str(folder/'.env'),'-p','project','-f',str(config),'up','-d','svc && strange']]
