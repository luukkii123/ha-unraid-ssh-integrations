"""Snapshot assembly and the stack/container merge."""

from __future__ import annotations

from unraid_ssh import collect, model, parse
from unraid_ssh.icons import IconSource


def test_normalize_project_name():
    assert model.normalize_project_name("Buschfunk") == "buschfunk"
    assert model.normalize_project_name("gps-bridge") == "gps-bridge"
    assert model.normalize_project_name("Claude Station!") == "claude-station"
    assert model.normalize_project_name("-lead") == "lead"


def _snap(stacks):
    """A snapshot carrying nothing but these stacks -- enough for the finders."""
    return model.Snapshot(
        array=None, disks=(), shares=(), cpu_times=None, cpu_percent=None, memory=None,
        load=None, gpus=(), stacks=tuple(stacks), template_containers=(), containers=(),
        vms=(), failed=frozenset(),
    )


def test_unraid_project_name_is_the_compose_manager_rule():
    """Unraid's own rule, not compose's: `.`, whitespace and `-` become `_`.

    Read off the plugin on the server: `sanitizeStr()` in
    /usr/local/emhttp/plugins/compose.manager/php/util.php and the project name
    in /usr/local/emhttp/plugins/compose.manager/php/compose_util.php. Confirmed
    against `docker compose ls`: the folder `gps-bridge` (no `name` file) runs as
    the project `gps_bridge`.
    """
    assert model.unraid_project_name("gps-bridge") == "gps_bridge"
    assert model.unraid_project_name("Claude Station") == "claude_station"
    assert model.unraid_project_name("Busch-Print") == "busch_print"
    assert model.unraid_project_name("Buschfunk") == "buschfunk"
    assert model.unraid_project_name("a.b c") == "a_b_c"


def test_merge_stacks_carries_the_compose_manager_project_name():
    """`manager_name` is what Unraid's own button would start the stack under.

    A folder-backed stack always has one -- computed from the same string
    Unraid uses, the `name` file if there is one and the folder basename
    otherwise. A stack that exists only in `docker compose ls` has none.
    """
    dirs = [
        parse.StackDir(path="/p/gps-bridge/", name="gps-bridge", autostart=True),
        parse.StackDir(path="/p/Claude-Station/", name="Claude Station", autostart=True),
    ]
    stacks, _ = model.merge_stacks(dirs, [], [])
    assert [s.manager_name for s in stacks] == ["gps_bridge", "claude_station"]

    projects = [parse.ComposeProject(name="adhoc", status="running(1)", running=1, config_files=("/tmp/a.yml",))]
    adhoc = model.merge_stacks([], projects, [])[0][0]
    assert adhoc.folder == "" and adhoc.manager_name == ""


def test_merge_stacks_matches_a_project_by_the_unraid_name():
    """`gps-bridge` on disk, `gps_bridge` in compose -- and the config file
    somewhere else entirely (a stack started by hand from another path). The
    Unraid name is what ties the two together; without it the stack would
    appear twice."""
    dirs = [parse.StackDir(path="/p/gps-bridge/", name="gps-bridge", autostart=True)]
    projects = [
        parse.ComposeProject(
            name="gps_bridge", status="running(1)", running=1,
            config_files=("/elsewhere/docker-compose.yml",),
        )
    ]
    stacks, _ = model.merge_stacks(dirs, projects, [])
    assert len(stacks) == 1
    assert stacks[0].name == "gps_bridge" and stacks[0].folder == "/p/gps-bridge/"
    assert stacks[0].present is True


def test_stack_key_is_the_same_whether_or_not_the_stack_runs():
    """The device identity of a stack must not depend on it running.

    `Stack.name` is the live project name while the stack is up and a derived
    name while it is down. Keying a device on that would give one stack two
    devices over its lifetime, the older one permanently unavailable. The
    folder basename does not move.
    """
    dirs = [parse.StackDir(path="/p/gps-bridge/", name="gps-bridge", autostart=True)]
    projects = [
        parse.ComposeProject(
            name="gps_bridge", status="running(1)", running=1,
            config_files=("/p/gps-bridge/docker-compose.yml",),
        )
    ]
    containers = [parse.Container("gps-bridge", "running", "img", "gps_bridge", "gps-bridge")]
    up = model.merge_stacks(dirs, projects, containers)[0][0]
    down = model.merge_stacks(dirs, [], [])[0][0]
    assert up.name != down.name                      # exactly the case that used to split the device
    assert model.stack_key(up) == model.stack_key(down) == "gps-bridge"

    # And the lookup an entity keeps doing has to use that key, not the name:
    # the switch of a downed stack must still find its stack.
    assert model.find_stack_by_key(_snap([down]), "gps-bridge") is down
    assert model.find_stack_by_key(_snap([up]), "gps-bridge") is up
    assert model.find_stack(_snap([down]), "gps_bridge") is None      # why the key exists

    # Only a stack without a folder has nothing but its project name.
    adhoc_projects = [parse.ComposeProject(name="adhoc", status="running(1)", running=1, config_files=("/tmp/a.yml",))]
    adhoc = model.merge_stacks([], adhoc_projects, [])[0][0]
    assert model.stack_key(adhoc) == "adhoc"


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
    assert gps.manager_name == "gps_bridge"          # what Unraid would start it as
    assert model.stack_key(gps) == "gps-bridge"
    finanz = model.find_stack(snap, "busch-finanz")
    assert finanz is not None and finanz.folder == "" and finanz.present is True
    assert any(c.name == "buschfunk-vorschau" for c in snap.template_containers)
    assert snap.vms and snap.vms[0].name == "Windows 11"
    assert snap.icon_sources == {
        "buschfunk-web-1": IconSource(
            "file", "/var/lib/docker/unraid/images/buschfunk-web.png", "123:456"
        )
    }
    assert snap.icons_valid is True
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


def test_build_snapshot_distinguishes_empty_icons_from_a_failed_icon_section(fixture):
    sections = collect.split_output(fixture("full_output.txt"))
    sections["icons"] = "{}\n"
    empty = model.build_snapshot(sections, None)
    assert empty.icon_sources == {}
    assert empty.icons_valid is True
    assert "icons" not in empty.failed

    sections["icons"] = "broken-json\n"
    broken = model.build_snapshot(sections, None)
    assert broken.icon_sources == {}
    assert broken.icons_valid is False
    assert "icons" in broken.failed
    assert broken.array is not None


def test_stack_switchable_only_with_a_compose_file():
    """A stack that only exists through container labels gets no on/off switch.

    `docker compose -p <name> down` would work there (it goes by label), `up -d`
    would not — there is no configuration file. Switching off would empty the
    project, and with no members left `merge_stacks` drops the stack entirely:
    switch, device and container switches gone for good. So the switch is not
    offered in the first place.
    """
    containers = [parse.Container("ghost-web-1", "running", "img", "ghost", "web")]
    ghost = model.merge_stacks([], [], containers)[0][0]
    assert ghost.folder == "" and ghost.config_files == ()
    assert model.stack_switchable(ghost) is False

    # A folder from compose.manager is enough (a downed stack has no config_files).
    dirs = [parse.StackDir(path="/p/Downed/", name="downed", autostart=False)]
    downed = model.merge_stacks(dirs, [], [])[0][0]
    assert downed.folder and downed.config_files == ()
    assert model.stack_switchable(downed) is True

    # And so is a compose project without a folder.
    projects = [parse.ComposeProject(name="adhoc", status="running(1)", running=1, config_files=("/tmp/a.yml",))]
    adhoc = model.merge_stacks([], projects, [])[0][0]
    assert adhoc.folder == "" and adhoc.config_files
    assert model.stack_switchable(adhoc) is True


def test_refs_to_check_and_update_state():
    inv = [parse.InventoryRow("a", "img/a:latest", "sha256:1"), parse.InventoryRow("b", "img/a:latest", "sha256:1"),
           parse.InventoryRow("c", "local-built", "sha256:2"), parse.InventoryRow("d", "img/d", "sha256:3")]
    digests = {"sha256:1": ("sha256:aaa",), "sha256:2": (), "sha256:3": ("sha256:ddd", "sha256:ddd2")}
    assert model.refs_to_check(inv, digests) == ["img/a:latest", "img/d"]
    state = model.build_update_state(inv, digests, {"img/a:latest": "sha256:aaa", "img/d": "sha256:new"})
    assert set(state) == {"a", "b", "d"}                     # c has no digest -> no entity
    assert state["a"].update_available is False and state["a"].local_digest == "sha256:aaa"
    assert state["d"].update_available is True and state["d"].remote_digest == "sha256:new"
    unknown = model.build_update_state(inv, digests, {})
    assert unknown["a"].remote_digest is None and unknown["a"].update_available is None


def test_container_updatable_only_where_the_update_command_can_work():
    """Install is offered exactly where one of the two update paths exists.

    A template container (no compose project) goes through Unraid's own
    `update_container` script -- that script knows only template containers.
    A compose container needs the files its stack was started with:
    `docker compose -p X pull svc` without any `-f` answers "no configuration
    file provided" (measured on the server), so a stack without config files
    cannot be updated that way. Offering install there would produce a button
    that always fails.
    """
    template = parse.Container("buschfunk-vorschau", "running", "nginx:alpine", "", "")
    assert model.container_updatable(template, None) is True

    web = parse.Container("buschfunk-web-1", "running", "img", "buschfunk", "web")
    with_files = model.Stack(name="buschfunk", manager_name="buschfunk", folder="/p/Buschfunk/",
                             autostart=True, present=True,
                             running=1, config_files=("/p/Buschfunk/docker-compose.yml",), containers=())
    assert model.container_updatable(web, with_files) is True

    # A downed compose.manager stack has a folder but no config files: compose
    # would have nothing to read.
    no_files = model.Stack(name="buschfunk", manager_name="buschfunk", folder="/p/Buschfunk/",
                           autostart=True, present=False,
                           running=0, config_files=(), containers=())
    assert model.container_updatable(web, no_files) is False

    # Orphan: the project label points at no stack we resolved.
    assert model.container_updatable(web, None) is False
