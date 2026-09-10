"""Snapshot assembly and the stack/container merge."""

from __future__ import annotations

from unraid_ssh import collect, model, parse


def test_normalize_project_name():
    assert model.normalize_project_name("Buschfunk") == "buschfunk"
    assert model.normalize_project_name("gps-bridge") == "gps-bridge"
    assert model.normalize_project_name("Claude Station!") == "claude-station"
    assert model.normalize_project_name("-lead") == "lead"


def test_merge_stacks_attaches_containers_and_keeps_downed_stacks():
    dirs = [
        parse.StackDir(path="/p/Buschfunk/", name="buschfunk", autostart=True),
        parse.StackDir(path="/p/Downed/", name="downed", autostart=False),
    ]
    projects = [parse.ComposeProject(name="buschfunk", status="running(2)", running=2, config_files=("/p/Buschfunk/docker-compose.yml",))]
    containers = [
        parse.Container("buschfunk-web-1", "running", "img", "buschfunk", "web"),
        parse.Container("buschfunk-kern-1", "running", "img", "buschfunk", "kern"),
        parse.Container("buschfunk-vorschau", "running", "nginx:alpine", "", ""),
    ]
    stacks, loose = model.merge_stacks(dirs, projects, containers)
    by_name = {s.name: s for s in stacks}
    assert set(by_name) == {"buschfunk", "downed"}
    assert by_name["buschfunk"].present is True and by_name["buschfunk"].running == 2
    assert {c.name for c in by_name["buschfunk"].containers} == {"buschfunk-web-1", "buschfunk-kern-1"}
    assert by_name["downed"].present is False and by_name["downed"].running == 0 and by_name["downed"].containers == ()
    assert [c.name for c in loose] == ["buschfunk-vorschau"]


def test_merge_stacks_project_without_folder_still_appears():
    projects = [parse.ComposeProject(name="adhoc", status="running(1)", running=1, config_files=("/tmp/a.yml",))]
    containers = [parse.Container("adhoc-x-1", "running", "img", "adhoc", "x")]
    stacks, loose = model.merge_stacks([], projects, containers)
    assert stacks[0].name == "adhoc" and stacks[0].folder == "" and stacks[0].containers[0].name == "adhoc-x-1"
    assert loose == ()


def test_merge_stacks_matches_project_by_config_file_path():
    """The folder name normalizes to `gps-bridge`, the compose project is `gps_bridge`.

    Only the config file under the folder ties the two together; without that
    rule the stack would appear twice — once empty, once folderless.
    """
    dirs = [parse.StackDir(path="/p/gps-bridge/", name="gps-bridge", autostart=True)]
    projects = [
        parse.ComposeProject(
            name="gps_bridge",
            status="running(1)",
            running=1,
            config_files=("/p/gps-bridge/docker-compose.yml",),
        )
    ]
    containers = [parse.Container("gps-bridge", "running", "img", "gps_bridge", "gps-bridge")]
    stacks, loose = model.merge_stacks(dirs, projects, containers)
    assert len(stacks) == 1
    assert stacks[0].name == "gps_bridge"
    assert stacks[0].folder == "/p/gps-bridge/"
    assert stacks[0].present is True and stacks[0].running == 1
    assert [c.name for c in stacks[0].containers] == ["gps-bridge"]
    assert loose == ()


def test_merge_stacks_keeps_containers_of_an_unknown_project():
    """No folder, no compose project — the containers still get a stack.

    Without this the containers would vanish entirely: not in any stack, not in
    the loose tuple. `present` stays False, because nothing in `compose ls`
    confirms the project.
    """
    containers = [
        parse.Container("ghost-web-1", "running", "img", "ghost", "web"),
        parse.Container("ghost-db-1", "exited", "img", "ghost", "db"),
        parse.Container("standalone", "running", "nginx:alpine", "", ""),
    ]
    stacks, loose = model.merge_stacks([], [], containers)
    assert len(stacks) == 1
    ghost = stacks[0]
    assert ghost.name == "ghost" and ghost.folder == "" and ghost.autostart is False
    assert ghost.present is False and ghost.running == 1 and ghost.config_files == ()
    assert {c.name for c in ghost.containers} == {"ghost-web-1", "ghost-db-1"}
    assert [c.name for c in loose] == ["standalone"]


def test_build_snapshot_from_recorded_output(fixture):
    sections = collect.split_output(fixture("full_output.txt"))
    snap = model.build_snapshot(sections, None)
    assert snap.array is not None and snap.array.name == "Unraid"
    assert snap.cpu_percent is None                      # first sample
    assert snap.memory is not None and snap.load is not None
    assert len(snap.disks) >= 5 and len(snap.shares) >= 1
    assert len(snap.stacks) == 14                        # 13 folders + folderless busch-finanz
    gps = next(s for s in snap.stacks if s.folder.rstrip("/").endswith("/gps-bridge"))
    assert any(c.name == "gps-bridge" for c in gps.containers)
    finanz = model.find_stack(snap, "busch-finanz")
    assert finanz is not None and finanz.folder == "" and finanz.present is True
    assert any(c.name == "buschfunk-vorschau" for c in snap.template_containers)
    assert snap.vms and snap.vms[0].name == "Windows 11"
    assert snap.failed == frozenset()

    second = model.build_snapshot(sections, snap)
    assert second.cpu_percent is None                    # identical counters: no elapsed time
    assert model.find_disk(second, "disk1") is not None
    assert model.find_disk(second, "nope") is None
    assert model.find_stack(second, "buschfunk") is not None
    assert model.find_vm(second, "Windows 11") is not None


def test_build_snapshot_loses_no_container_when_compose_fails(fixture):
    """`compose` unusable, `docker` fine: every container keeps a home.

    Without the synthetic stacks the `gps_bridge` containers would be gone —
    only the config-file path could have matched them to their folder, and that
    path comes from the section that just failed.
    """
    sections = collect.split_output(fixture("full_output.txt"))
    intact = model.build_snapshot(sections, None)
    sections["compose"] = "garbage"
    snap = model.build_snapshot(sections, None)
    attached = sum(len(s.containers) for s in snap.stacks)
    assert attached + len(snap.template_containers) == len(snap.containers)
    assert len(snap.containers) == len(intact.containers)
    assert model.find_stack(snap, "gps_bridge") is not None
    assert model.find_stack(snap, "gps_bridge").present is False


def test_build_snapshot_isolates_a_broken_section(fixture):
    sections = collect.split_output(fixture("full_output.txt"))
    sections["stat"] = "garbage"
    sections["gpu"] = ""
    snap = model.build_snapshot(sections, None)
    assert "stat" in snap.failed
    assert snap.cpu_times is None
    assert snap.gpus == ()
    assert snap.array is not None                        # the rest survived
