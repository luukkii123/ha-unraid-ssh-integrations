"""The command chain and the splitting of its output."""

from __future__ import annotations

import json
import shlex
import subprocess

import pytest

from unraid_ssh import collect


def test_state_command_contains_every_section_in_order():
    cmd = collect.build_state_command()
    names = [name for name, _ in collect.SECTIONS]
    assert names == ["var", "disks", "shares", "stat", "mem", "load", "gpu", "docker", "icons", "compose", "stacks", "vms"]
    positions = [cmd.index(f"echo '@@@ {name}'") for name in names]
    assert positions == sorted(positions)
    assert cmd.rstrip().endswith("echo '@@@ end'")
    assert "set -e" not in cmd                    # one failing section must not kill the rest


def test_state_command_uses_the_recorded_shapes():
    cmd = collect.build_state_command()
    assert "/var/local/emhttp/var.ini" in cmd
    assert "--format=csv,noheader,nounits" in cmd
    assert '{{.Label "com.docker.compose.project"}}' in cmd
    assert '{{json (.Label "net.unraid.docker.icon")}}' in cmd
    assert "docker compose ls -a --format json" in cmd
    assert "virsh list --all" in cmd


def test_icon_metadata_command_is_one_fixed_quoted_php_program():
    command = dict(collect.SECTIONS)["icons"]
    argv = shlex.split(command)

    assert argv[:2] == ["php", "-r"]
    assert len(argv) == 7
    assert argv[3:] == [
        "/var/local/emhttp/plugins/dynamix.docker.manager/docker.json",
        "/usr/local/emhttp",
        "/usr/local/emhttp/state/plugins/dynamix.docker.manager/images",
        "/var/lib/docker/unraid/images",
    ]


def _run_icon_metadata(tmp_path, metadata: str):
    metadata_path = tmp_path / "docker.json"
    metadata_path.write_text(metadata, encoding="utf-8")
    emhttp_root = tmp_path / "emhttp"
    state_root = emhttp_root / "state/plugins/dynamix.docker.manager/images"
    docker_root = tmp_path / "docker-images"
    state_root.mkdir(parents=True, exist_ok=True)
    docker_root.mkdir(exist_ok=True)
    command = collect.build_icon_metadata_command(
        metadata_path=str(metadata_path),
        emhttp_root=str(emhttp_root),
        cache_roots=(str(state_root), str(docker_root)),
    )
    return subprocess.run(
        shlex.split(command),
        check=False,
        capture_output=True,
        text=True,
    ), state_root


def test_icon_metadata_php_preserves_an_empty_json_object(tmp_path):
    completed, _ = _run_icon_metadata(tmp_path, "{}")

    assert completed.returncode == 0
    assert completed.stderr == ""
    assert completed.stdout == "{}"


def test_icon_metadata_php_keeps_object_shape_when_every_record_is_rejected(tmp_path):
    metadata = json.dumps(
        {
            "generic": {"icon": "/state/plugins/dynamix.docker.manager/images/question.png"},
            "empty": {"icon": ""},
            "wrong_type": {"icon": 12},
        }
    )
    completed, _ = _run_icon_metadata(tmp_path, metadata)

    assert completed.returncode == 0
    assert completed.stderr == ""
    assert completed.stdout == "{}"


def test_icon_metadata_php_rejects_a_top_level_json_array(tmp_path):
    completed, _ = _run_icon_metadata(tmp_path, "[]")

    assert completed.returncode != 0
    assert completed.stdout == ""


def test_icon_metadata_php_keeps_numeric_container_names_as_object_keys(tmp_path):
    state_root = tmp_path / "emhttp/state/plugins/dynamix.docker.manager/images"
    state_root.mkdir(parents=True)
    icon = state_root / "numeric.png"
    icon.write_bytes(b"synthetic image bytes")
    metadata = json.dumps(
        {
            "123": {
                "icon": "/state/plugins/dynamix.docker.manager/images/numeric.png"
            }
        }
    )
    completed, _ = _run_icon_metadata(tmp_path, metadata)

    assert completed.returncode == 0
    assert isinstance(json.loads(completed.stdout), dict)
    assert set(json.loads(completed.stdout)) == {"123"}


def test_split_output(fixture):
    sections = collect.split_output(fixture("full_output.txt"))
    assert set(sections) >= {name for name, _ in collect.SECTIONS}
    assert 'mdState="' in sections["var"]
    assert sections["vms"].strip().endswith("shut off") or "running" in sections["vms"]


def test_split_output_without_end_marker_is_truncated():
    with pytest.raises(collect.TruncatedOutput):
        collect.split_output("@@@ var\nNAME=\"x\"\n@@@ disks\n")


def test_split_output_keeps_empty_sections():
    out = collect.split_output("@@@ var\nNAME=\"x\"\n@@@ gpu\n@@@ end\n")
    assert out["gpu"] == ""
    assert out["var"] == 'NAME="x"\n'


def test_split_output_survives_a_section_without_a_trailing_newline():
    """`echo '@@@ end'` writes straight after whatever the last command left.

    A section whose output has no final newline used to glue the next marker
    onto its last line -- the marker then started no new section, `@@@ end` was
    never seen, and the whole poll failed as truncated. One section without a
    trailing newline must not cost more than that section's last line ending.
    """
    out = collect.split_output('@@@ var\nNAME="x"\n@@@ stat\ncpu 1 2 3@@@ end\n')
    assert out["var"] == 'NAME="x"\n'
    assert out["stat"] == "cpu 1 2 3"


def test_inventory_command_shape():
    cmd = collect.build_inventory_command()
    assert "echo '@@@ containers'" in cmd and "echo '@@@ images'" in cmd and cmd.rstrip().endswith("echo '@@@ end'")
    cmd = "\n".join(shlex.split(cmd))  # Decode the Bash command argument.
    # A literal `\t` stays literal in `docker inspect --format` (recorded fixture,
    # docker 29.5.3), so the separator must be the template action `{{"\t"}}`.
    assert (
        "docker inspect --format "
        "'{{.Name}}{{\"\\t\"}}{{.Config.Image}}{{\"\\t\"}}{{.Image}}'" in cmd
    )
    assert "docker image inspect --format '{{.Id}}{{\"\\t\"}}{{join .RepoDigests \",\"}}'" in cmd


def test_remote_digest_command_quotes_refs_and_uses_timeout():
    cmd = collect.build_remote_digest_command(["lscr.io/linuxserver/radarr:latest", "odd ref"])
    assert cmd.startswith("echo '@@@ remote'; for r in lscr.io/linuxserver/radarr:latest 'odd ref'; do ")
    assert "timeout 30 docker buildx imagetools inspect \"$r\" --format '{{.Manifest.Digest}}'" in cmd
    assert cmd.rstrip().endswith("done; echo '@@@ end'")
    assert collect.build_remote_digest_command([]).startswith("echo '@@@ remote'; echo '@@@ end'")


@pytest.mark.parametrize('section', ['docker', 'compose', 'stacks', 'gpu', 'shares', 'disks', 'vms', 'icons'])
@pytest.mark.parametrize('command', ["printf partial; exit 7", "printf partial; false | cat"])
def test_failed_section_is_discarded_before_snapshot(monkeypatch, section, command):
    from unraid_ssh.model import build_snapshot
    monkeypatch.setattr(collect, 'SECTIONS', ((section, command), ('other', 'printf healthy')))
    result = subprocess.run(['/bin/sh', '-c', collect.build_state_command()], capture_output=True, text=True, check=True)
    sections = collect.split_output(result.stdout)
    assert section not in sections
    assert sections['other'] == 'healthy'
    assert section in build_snapshot(sections, None).failed


@pytest.mark.parametrize('section', ['docker', 'compose', 'stacks', 'gpu', 'shares', 'disks', 'vms'])
def test_successful_empty_section_remains_valid(monkeypatch, section):
    from unraid_ssh.model import build_snapshot
    monkeypatch.setattr(collect, 'SECTIONS', ((section, 'true'),))
    result = subprocess.run(['/bin/sh', '-c', collect.build_state_command()], capture_output=True, text=True, check=True)
    sections = collect.split_output(result.stdout)
    assert sections[section] == ''
    assert section not in build_snapshot(sections, None).failed


@pytest.mark.parametrize('text', ['broken', '{}', 'null', '[1]'])
def test_invalid_compose_json_is_not_confirmed_empty(text):
    from unraid_ssh.model import build_snapshot
    assert 'compose' in build_snapshot({'compose': text}, None).failed


@pytest.mark.parametrize('failure', ['none', 'ps', 'inspect', 'image_inspect', 'empty'])
def test_inventory_propagates_nested_failures_and_empty_success(tmp_path, failure):
    fake = tmp_path / 'docker'
    fake.write_text('''#!/bin/sh
case "$1 $2" in
 'ps -aq') [ "$FAIL_AT" = ps ] && exit 7; [ "$FAIL_AT" = empty ] || printf 'id';;
 'inspect --format') [ "$FAIL_AT" = inspect ] && exit 8; printf 'image';;
 'image inspect') [ "$FAIL_AT" = image_inspect ] && exit 9; printf 'digest';;
esac
exit 0
''')
    fake.chmod(0o755)
    import os
    env = dict(os.environ, PATH=str(tmp_path) + ':' + os.environ['PATH'], FAIL_AT=failure)
    result = subprocess.run(['/bin/sh', '-c', collect.build_inventory_command()], env=env, capture_output=True, text=True, check=True)
    sections = collect.split_output(result.stdout)
    if failure in ('ps', 'inspect'):
        assert 'containers' not in sections and 'images' not in sections
    elif failure == 'image_inspect':
        assert sections['containers'] == 'image' and 'images' not in sections
    elif failure == 'empty':
        assert sections == {'containers': '', 'images': ''}
    else:
        assert sections == {'containers': 'image', 'images': 'digest'}


@pytest.mark.parametrize('metadata', ['absent', 'valid', 'unreadable'])
def test_stack_loop_propagates_existing_file_read_errors(tmp_path, monkeypatch, metadata):
    from unraid_ssh.const import COMPOSE_PROJECTS_DIR
    directory = tmp_path / 'stacks'
    stack = directory / 'Example'
    stack.mkdir(parents=True)
    if metadata == 'valid':
        (stack / 'name').write_text('Example')
        (stack / 'autostart').write_text('yes')
    elif metadata == 'unreadable':
        (stack / 'name').mkdir()  # cat fails even when the test runs as root.
    command = dict(collect.SECTIONS)['stacks'].replace(COMPOSE_PROJECTS_DIR, shlex.quote(str(directory)))
    monkeypatch.setattr(collect, 'SECTIONS', (('stacks', command),))
    result = subprocess.run(['/bin/sh', '-c', collect.build_state_command()], capture_output=True, text=True, check=True)
    sections = collect.split_output(result.stdout)
    if metadata == 'unreadable':
        assert 'stacks' not in sections
    else:
        assert str(stack) in sections['stacks']
