"""Parsers against recorded output of the real server (tests/fixtures)."""

from __future__ import annotations

import pytest

from unraid_ssh import collect, parse


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


def test_parse_proc_stat_and_cpu_percent(fixture):
    cur = parse.parse_proc_stat(fixture("stat.txt"))
    assert cur.total > cur.idle > 0
    assert parse.cpu_percent(None, cur) is None
    prev = parse.CpuTimes(total=cur.total - 1000, idle=cur.idle - 250)
    assert parse.cpu_percent(prev, cur) == 75.0
    assert parse.cpu_percent(cur, cur) is None          # no elapsed time


def test_parse_meminfo(fixture):
    mem = parse.parse_meminfo(fixture("meminfo.txt"))
    assert mem.total_kib > mem.available_kib > 0
    assert 0 < mem.percent < 100


def test_parse_load(fixture):
    load = parse.parse_load(fixture("load.txt"))
    assert load.load1 >= 0 and load.load5 >= 0 and load.load15 >= 0
    assert load.uptime_seconds > 60


def test_parse_gpus(fixture):
    gpus = parse.parse_gpus(fixture("gpu.csv"))
    assert len(gpus) >= 1
    gpu = gpus[0]
    assert gpu.index == 0
    assert "NVIDIA" in gpu.name
    assert 0 <= gpu.util_percent <= 100
    assert gpu.vram_total_mib > 0
    assert gpu.vram_percent == round(gpu.vram_used_mib / gpu.vram_total_mib * 100, 1)
    assert gpu.temp > 0 and gpu.power_w > 0


def test_parse_gpus_empty():
    assert parse.parse_gpus("") == []
    assert parse.parse_gpus("No devices were found\n") == []


def test_parse_containers(fixture):
    containers = parse.parse_containers(fixture("docker.tsv"))
    assert len(containers) > 40
    by_name = {c.name: c for c in containers}
    vorschau = by_name["buschfunk-vorschau"]
    assert vorschau.project == "" and vorschau.service == ""
    web = by_name["buschfunk-web-1"]
    assert web.project == "buschfunk" and web.service == "web"
    assert web.state in ("running", "exited", "created", "paused", "restarting", "dead")


def test_docker_icon_label_is_optional_and_json_encoded():
    old = parse.parse_containers("web\trunning\texample/web:latest\tstack\tweb")[0]
    new = parse.parse_containers(
        'web\trunning\texample/web:latest\tstack\tweb\t"https://example.com/web.png"'
    )[0]
    empty = parse.parse_containers('web\trunning\texample/web:latest\tstack\tweb\t""')[0]
    malformed = parse.parse_containers(
        "web\trunning\texample/web:latest\tstack\tweb\tnot-json"
    )[0]

    assert old.icon is None
    assert new.icon == "https://example.com/web.png"
    assert empty.icon is None
    assert malformed.icon is None


def test_parse_containers_discards_a_json_label_with_the_wrong_type():
    container = parse.parse_containers(
        "web\trunning\texample/web:latest\tstack\tweb\t123"
    )[0]
    assert container.icon is None


def test_parse_compose_ls(fixture):
    projects = parse.parse_compose_ls(fixture("compose.json"))
    names = {p.name for p in projects}
    assert "buschfunk" in names
    bf = next(p for p in projects if p.name == "buschfunk")
    assert bf.running >= 1
    assert any(f.endswith("docker-compose.yml") for f in bf.config_files)


def test_parse_compose_ls_status_forms():
    text = '[{"Name":"a","Status":"running(3)","ConfigFiles":"/x/a.yml"},' \
           '{"Name":"b","Status":"exited(1), running(7)","ConfigFiles":"/x/b.yml,/x/b2.yml"},' \
           '{"Name":"c","Status":"exited(2)","ConfigFiles":""}]'
    a, b, c = parse.parse_compose_ls(text)
    assert (a.running, b.running, c.running) == (3, 7, 0)
    assert b.config_files == ("/x/b.yml", "/x/b2.yml")
    assert c.config_files == ()
    assert parse.parse_compose_ls("") == []


def test_parse_stack_dirs(fixture):
    stacks = parse.parse_stack_dirs(fixture("stacks.tsv"))
    assert len(stacks) == 13
    bf = next(s for s in stacks if s.path.rstrip("/").endswith("/Buschfunk"))
    assert bf.name == "Buschfunk"      # the `name` file of that folder, read from the fixture
    assert isinstance(bf.autostart, bool)
    gps = next(s for s in stacks if s.path.rstrip("/").endswith("/gps-bridge"))
    assert gps.name == "gps-bridge"    # empty `name` file -> folder name
    dominion = next(s for s in stacks if s.path.rstrip("/").endswith("/Dominion"))
    assert dominion.autostart is False  # empty `autostart` file


def test_parse_stack_dirs_without_name_file():
    (s,) = parse.parse_stack_dirs("/boot/config/plugins/compose.manager/projects/Foo/\t\t\n")
    assert s.name == "Foo"            # falls back to folder name
    assert s.autostart is False


def test_parse_vms(fixture):
    vms = parse.parse_vms(fixture("vms.txt"))
    assert vms == [parse.Vm(name="Windows 11", state="shut_off")]


def test_parse_vms_running_and_empty():
    text = " Id   Name         State\n-----------------------------\n 3    Windows 11   running\n"
    assert parse.parse_vms(text) == [parse.Vm(name="Windows 11", state="running")]
    assert parse.parse_vms("") == []
    assert parse.vm_state("in shutdown") == "in_shutdown"
    assert parse.vm_state("weird") == "unknown"


def test_parse_inventory_and_image_digests(fixture):
    sections = collect.split_output(fixture("inventory.txt"))
    rows = parse.parse_inventory(sections["containers"])
    assert rows and not rows[0].name.startswith("/")
    by_name = {r.name: r for r in rows}
    assert by_name["buschfunk-web-1"].image_ref == "buschfunk-web"
    assert by_name["buschfunk-web-1"].image_id.startswith("sha256:")
    digests = parse.parse_image_digests(sections["images"])
    assert digests[by_name["buschfunk-web-1"].image_id] == ()          # locally built: no repo digest
    radarr = by_name["radarr"]
    assert all(d.startswith("sha256:") for d in digests[radarr.image_id]) and digests[radarr.image_id]


def test_parse_remote_digests(fixture):
    sections = collect.split_output(fixture("remote.txt"))
    remote = parse.parse_remote_digests(sections["remote"])
    assert "ghcr.io/esphome/esphome:stable" in remote
    assert remote["ghcr.io/esphome/esphome:stable"].startswith("sha256:")
    assert parse.parse_remote_digests("some/ref\t\n") == {"some/ref": None}
