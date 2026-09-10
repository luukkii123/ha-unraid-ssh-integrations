"""The command chain and the splitting of its output."""

from __future__ import annotations

import pytest

from unraid_ssh import collect


def test_state_command_contains_every_section_in_order():
    cmd = collect.build_state_command()
    names = [name for name, _ in collect.SECTIONS]
    assert names == ["var", "disks", "shares", "stat", "mem", "load", "gpu", "docker", "compose", "stacks", "vms"]
    positions = [cmd.index(f"echo '@@@ {name}'") for name in names]
    assert positions == sorted(positions)
    assert cmd.rstrip().endswith("echo '@@@ end'")
    assert "set -e" not in cmd                    # one failing section must not kill the rest


def test_state_command_uses_the_recorded_shapes():
    cmd = collect.build_state_command()
    assert "/var/local/emhttp/var.ini" in cmd
    assert "--format=csv,noheader,nounits" in cmd
    assert '{{.Label "com.docker.compose.project"}}' in cmd
    assert "docker compose ls -a --format json" in cmd
    assert "virsh list --all" in cmd


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
