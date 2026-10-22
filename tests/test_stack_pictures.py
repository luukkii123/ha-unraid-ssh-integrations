"""Stack pictures and display names: compose order, labels of downed stacks, http fetch."""

from __future__ import annotations

import base64
import os
import shlex
import stat
import subprocess

from unraid_ssh import collect, icons, model, parse
from unraid_ssh.icons import IconSource
from unraid_ssh.parse import Container, StackServices

FOLDER = "/boot/config/plugins/compose.manager/projects/Webstack/"


def line(folder, name, text):
    return f"{folder}\t{name}\t{base64.b64encode(text.encode()).decode()}"


MAIN = """services:
  web:
    image: example/web
  db:
    image: example/db
    labels:
      net.unraid.docker.icon: https://example.com/db-main.png
  worker:
    image: example/worker
"""
OVERRIDE = """services:
  db:
    labels:
      net.unraid.docker.icon: 'https://example.com/db.png'
  web:
    labels:
      - "net.unraid.docker.icon=http://app.example/icon.png"
  extra:
    labels:
      net.unraid.docker.icon: ''
  tool:
    labels: [ "other=1" ]
"""


def test_services_keep_compose_order_and_override_labels_win():
    parsed = parse.parse_stack_services("\n".join([line(FOLDER, "docker-compose.yml", MAIN),
                                                   line(FOLDER, "docker-compose.override.yml", OVERRIDE)]))
    services = parsed[FOLDER]
    assert services.services == ("web", "db", "worker", "extra", "tool")
    assert services.icons == {"web": "http://app.example/icon.png", "db": "https://example.com/db.png"}


def test_broken_input_only_drops_its_own_folder():
    other = "/boot/config/plugins/compose.manager/projects/Other/"
    parsed = parse.parse_stack_services("\n".join([
        line(FOLDER, "docker-compose.yml", "services: [unclosed"),
        line(other, "docker-compose.yml", "services:\n  app:\n    labels: {net.unraid.docker.icon: 7}\n"),
        f"{other}\tdocker-compose.yml\tnot base64!",
        line(other, "evil.yml", "services: {x: {}}"),
        line(other, "docker-compose.override.yml", "x" * (parse.MAX_COMPOSE_FILE_BYTES + 1)),
        "short line",
        "",
    ]))
    assert FOLDER not in parsed
    assert parsed[other] == StackServices(("app",), {})
    assert parse.parse_stack_services(line(other, "docker-compose.yml", "- just a list")) == {other: StackServices(())}
    assert parse.parse_stack_services(line(other, "docker-compose.yml", "")) == {other: StackServices(())}
    assert parse.parse_stack_services(line(other, "docker-compose.yml", "services:\n  a:\n    labels: [net.unraid.docker.icon]\n")) == {
        other: StackServices(("a",), {})}


def test_loop_reads_both_files_of_every_folder(tmp_path):
    projects = tmp_path / "projects"
    (projects / "Webstack").mkdir(parents=True)
    (projects / "Empty").mkdir()
    (projects / "Webstack" / "docker-compose.yml").write_text(MAIN)
    (projects / "Webstack" / "docker-compose.override.yml").write_text(OVERRIDE)
    script = collect._STACK_FILES_LOOP.replace(collect.COMPOSE_PROJECTS_DIR, str(projects))
    done = subprocess.run(["bash", "-o", "pipefail", "-c", script], capture_output=True, text=True, check=True)
    parsed = parse.parse_stack_services(done.stdout)
    assert parsed[f"{projects}/Webstack/"].services[:2] == ("web", "db")
    assert f"{projects}/Empty/" not in parsed


def test_snapshot_carries_display_name_services_and_keeps_last_reading():
    sections = {
        "stacks": f"{FOLDER}\tWeb Stack\ttrue\n",
        "compose": "[]",
        "docker": "",
        "stack_files": line(FOLDER, "docker-compose.yml", MAIN),
    }
    first = model.build_snapshot(sections, None)
    stack = first.stacks[0]
    assert stack.display_name == "Web Stack" and stack.services.services == ("web", "db", "worker")
    assert "stack_files" not in first.failed
    attrs = model.stack_metadata(stack, "entry", "control")
    assert attrs["stack_display_name"] == "Web Stack" and attrs["stack_name"] == "web-stack"
    without = model.build_snapshot({k: v for k, v in sections.items() if k != "stack_files"}, first)
    assert without.stacks[0].services == stack.services and "stack_files" not in without.failed
    cold = model.build_snapshot({k: v for k, v in sections.items() if k != "stack_files"}, None)
    assert cold.stacks[0].services is None


def test_parser_crash_keeps_last_reading(monkeypatch):
    sections = {"stacks": f"{FOLDER}\t\ttrue\n", "compose": "[]", "docker": "", "stack_files": line(FOLDER, "docker-compose.yml", MAIN)}
    first = model.build_snapshot(sections, None)

    def boom(text):
        raise ImportError("no yaml")

    monkeypatch.setattr(parse, "parse_stack_services", boom)
    second = model.build_snapshot(sections, first)
    assert second.stacks[0].services == first.stacks[0].services
    assert second.stacks[0].display_name == "Webstack"


def test_container_metadata_names_its_stack():
    container = Container("webstack-web-1", "running", "example/web", "webstack", "web")
    sections = {"stacks": f"{FOLDER}\tWeb Stack\ttrue\n", "compose": "[]", "docker": ""}
    snapshot = model.build_snapshot(sections, None)
    stack = model.replace(snapshot.stacks[0], name="webstack", containers=(container,))
    snapshot = model.replace(snapshot, stacks=(stack,), containers=(container,))
    assert model.container_metadata(snapshot, container, "entry", "control")["stack_display_name"] == "Web Stack"
    loose = Container("solo", "running", "example/solo", "", "", managed="dockerman")
    assert model.container_metadata(snapshot, loose, "entry", "control")["stack_display_name"] is None


def c(name, service, icon=None):
    return Container(name, "running", "example/" + service, "webstack", service, icon)


def test_stack_icon_follows_compose_order_and_prefers_member_pictures():
    services = StackServices(("web", "db", "worker"), {"db": "https://example.com/db.png", "worker": "https://example.com/w.png"})
    cached = {"webstack-db-1": IconSource("file", "/var/lib/docker/unraid/images/db.png", "1:2")}
    members = (c("webstack-worker-1", "worker"), c("webstack-db-1", "db"), c("webstack-web-1", "web"))
    assert icons.select_stack_icon(members, services, cached) == cached["webstack-db-1"]
    assert icons.select_stack_icon(members, services, {}) == IconSource("url", "https://example.com/db.png", "")
    # Down: no containers, the compose label still names the picture.
    assert icons.select_stack_icon((), services, {}) == IconSource("url", "https://example.com/db.png", "")
    # A live label of a running member wins over the compose files of its service.
    live = (c("webstack-web-1", "web", "https://example.com/live.png"),)
    assert icons.select_stack_icon(live, services, {}).value == "https://example.com/live.png"
    # Members the compose files do not list, and stacks without compose files.
    extra = (c("webstack-runtime-1", "runtime", "https://example.com/rt.png"),)
    assert icons.select_stack_icon(extra, StackServices(("web",)), {}).value == "https://example.com/rt.png"
    assert icons.select_stack_icon(extra, None, {}).value == "https://example.com/rt.png"
    assert icons.select_stack_icon((c("a", "a"),), StackServices(("a",), {"a": "ftp://x/a.png"}), {}) is None


def test_only_plain_http_label_urls_are_fetched_by_the_server():
    assert icons.is_plain_http(IconSource("url", "HTTP://app.example/icon.png", ""))
    assert not icons.is_plain_http(IconSource("url", "https://example.com/a.png", ""))
    assert not icons.is_plain_http(IconSource("file", "/var/lib/docker/unraid/images/a.png", "1:1"))
    assert not icons.is_plain_http(None)


def test_fetch_command_quotes_the_url_and_reports_http_errors(tmp_path):
    url = "http://app.example/icon.png?a=1;touch${IFS}pwned&$(id)"
    command = icons.build_icon_fetch_command(url)
    assert shlex.split(command)[-1] == url
    fake = tmp_path / "curl"
    fake.write_text('#!/bin/bash\nfor a; do last="$a"; done\n'
                    'if [ "$last" = "http://broken.example/x.png" ]; then exit 22; fi\n'
                    'printf "%s" "$last"\n')
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}
    done = subprocess.run(command, shell=True, capture_output=True, text=True, env=env, cwd=tmp_path)
    assert done.returncode == 0 and base64.b64decode(done.stdout).decode() == url
    assert not (tmp_path / "pwned").exists()
    broken = subprocess.run(icons.build_icon_fetch_command("http://broken.example/x.png"), shell=True,
                            capture_output=True, text=True, env=env)
    assert broken.returncode == icons.ICON_FETCH_HTTP_ERROR
