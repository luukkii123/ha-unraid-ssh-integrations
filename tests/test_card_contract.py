"""Stable metadata, ambiguity and real restart command contracts."""
from dataclasses import replace
import pytest
from unraid_ssh import actions, model, parse


def snapshot(*containers):
    stack = model.Stack('live_project', 'stable', '/stacks/stable', False, True, 1,
                        ('/stacks/stable/compose.yaml',), containers)
    return replace(model.build_snapshot({}, None), stacks=(stack,), containers=containers, failed=frozenset())


def test_compose_key_survives_docker_recreation_and_project_name_change():
    c = parse.parse_containers('web-old\trunning\timg\tlive_project\tweb\tnull\t1')[0]
    assert c.replica == 1
    before = model.container_metadata(snapshot(c), c, 'entry', 'control')
    renamed = replace(c, name='web-new', project='new_project')
    after_snapshot = snapshot(renamed)
    after_snapshot = replace(after_snapshot, stacks=(replace(after_snapshot.stacks[0], name='new_project'),))
    after = model.container_metadata(after_snapshot, renamed, 'entry', 'update')
    assert before['container_key'] == after['container_key']
    assert before['identity_status'] == 'stable'
    assert before['role'] == 'control' and after['role'] == 'update'
    assert before['config_entry_id'] == after['config_entry_id'] == 'entry'
    assert before['stack_key'] == 'stable'


def test_missing_or_duplicate_replica_is_not_guessed():
    c = parse.Container('one', 'running', 'img', 'live_project', 'web')
    assert model.container_metadata(snapshot(c), c, 'entry', 'control')['identity_status'] == 'legacy'
    a = replace(c, replica=1)
    b = replace(a, name='transient-hash')
    first = model.container_metadata(snapshot(a,b), a, 'entry', 'control')
    second = model.container_metadata(snapshot(a,b), b, 'entry', 'control')
    assert first['identity_status'] == second['identity_status'] == 'ambiguous'
    assert first['container_key'] != second['container_key']


@pytest.mark.parametrize('replica', ['', 'bad', '0', '-1'])
def test_invalid_replicas_keep_legacy_identity(replica):
    c = parse.parse_containers('web\trunning\timg\tlive_project\tweb\tnull\t'+replica)[0]
    assert c.replica is None


def test_native_restart_commands_are_quoted_and_use_running_project():
    assert actions.container_restart_cmd('odd;name') == "docker restart 'odd;name'"
    stack = snapshot().stacks[0]
    command = actions.stack_restart_cmd(stack)
    assert ' -p live_project ' in command
    assert ' -f /stacks/stable/compose.yaml restart' in command
    assert ' stop ' not in command and ' start ' not in command and ' down' not in command
    assert '--env-file' in command
    with pytest.raises(ValueError):
        actions.stack_restart_cmd(replace(stack, config_files=()))
