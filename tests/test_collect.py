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
