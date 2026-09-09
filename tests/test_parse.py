"""Parsers against recorded output of the real server (tests/fixtures)."""

from __future__ import annotations

import pytest

from unraid_ssh import parse


def test_parse_flat_ini_strips_quotes():
    assert parse.parse_flat_ini('NAME="Unraid"\nPORT="9000"\n') == {"NAME": "Unraid", "PORT": "9000"}


def test_parse_sectioned_ini_handles_quoted_section_names():
    text = '["parity"]\nidx="0"\n["disk1"]\nidx="1"\ntemp="*"\n'
    out = parse.parse_sectioned_ini(text)
    assert list(out) == ["parity", "disk1"]
    assert out["disk1"]["temp"] == "*"


def test_parse_var(fixture):
    info = parse.parse_var(fixture("var.ini"))
    assert info.name == "Unraid"
    assert info.version == "7.3.2"
    assert info.md_state == "started"
    assert info.array_started is True
    assert info.parity_running is False
    assert info.parity_progress is None
    assert info.parity_errors == 0
    assert info.mover_active is False


def test_parse_var_parity_progress():
    text = 'mdState="STARTED"\nmdResync="1000"\nmdResyncPos="250"\nsbSyncErrs="3"\nshareMoverActive="yes"\n'
    info = parse.parse_var(text)
    assert info.parity_running is True
    assert info.parity_progress == 25.0
    assert info.parity_errors == 3
    assert info.mover_active is True


def test_parse_disks_skips_slots_without_device(fixture):
    disks = parse.parse_disks(fixture("disks.ini"))
    names = [d.name for d in disks]
    assert "parity" not in names          # empty slot: device=""
    assert "disk1" in names
    disk1 = next(d for d in disks if d.name == "disk1")
    assert disk1.device == "sdc"
    assert disk1.kind == "Data"
    assert disk1.status == "ok"
    assert disk1.temp == 44               # value read from the recorded fixture
    assert disk1.spundown is False
    assert disk1.fs_size_kib > 0
    assert disk1.usage_percent is not None and 0 <= disk1.usage_percent <= 100
    assert disk1.errors == 0


def test_parse_disks_star_temp_and_zero_fs():
    text = ('["cache"]\nname="cache"\ndevice="nvme0n1"\ntype="Cache"\nstatus="DISK_OK"\n'
            'temp="*"\nspundown="1"\nfsSize="0"\nfsFree="0"\nnumErrors="0"\nfsStatus="Mounted"\n')
    (disk,) = parse.parse_disks(text)
    assert disk.temp is None
    assert disk.spundown is True
    assert disk.usage_percent is None


@pytest.mark.parametrize(
    "raw,expected",
    [("DISK_OK", "ok"), ("DISK_NP_DSBL", "not_present"), ("DISK_DSBL", "disabled"),
     ("DISK_INVALID", "invalid"), ("DISK_NP", "not_present"), ("DISK_NEW", "new"),
     ("DISK_NP_MISSING", "missing"), ("SOMETHING_ELSE", "unknown")],
)
def test_disk_status_mapping(raw, expected):
    assert parse.disk_status(raw) == expected


def test_parse_shares(fixture):
    shares = parse.parse_shares(fixture("shares.ini"))
    assert shares, "fixture has at least one share"
    backups = next(s for s in shares if s.name == "Backups")
    assert backups.used_kib == 27283633016   # values read from the recorded fixture
    assert backups.free_kib == 5779811540
