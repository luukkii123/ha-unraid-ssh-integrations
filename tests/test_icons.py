"""Validation and selection of the existing Unraid container icon sources."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from unraid_ssh.icons import IconSource, parse_icon_metadata, select_icon
from unraid_ssh.parse import Container


def test_icon_source_is_immutable_and_rejects_unknown_kinds():
    source = IconSource("file", "/var/lib/docker/unraid/images/web-icon.png", "123:64")
    with pytest.raises(FrozenInstanceError):
        source.kind = "url"  # type: ignore[misc]
    with pytest.raises(ValueError, match="kind"):
        IconSource("data", "value", "revision")


def test_parse_icon_metadata_keeps_only_safe_cache_files():
    text = """{
        "web": {
            "path": "/var/lib/docker/unraid/images/web-icon.png",
            "revision": "123:64"
        },
        "state": {
            "path": "/usr/local/emhttp/state/plugins/dynamix.docker.manager/images/state.png",
            "revision": "456:128"
        },
        "canonical": {
            "path": "/mnt/synthetic-cache-root/canonical.png",
            "revision": "789:256"
        },
        "question": {
            "path": "/var/lib/docker/unraid/images/question.png",
            "revision": "1:1"
        },
        "relative": {"path": "relative/outside.png", "revision": "1:1"},
        "": {"path": "/var/lib/docker/unraid/images/unnamed.png", "revision": "1:1"},
        "bad_record": "not-an-object",
        "bad_path": {"path": 12, "revision": "1:1"},
        "bad_revision": {
            "path": "/var/lib/docker/unraid/images/bad.png",
            "revision": false
        }
    }"""

    assert parse_icon_metadata(text) == {
        "web": IconSource("file", "/var/lib/docker/unraid/images/web-icon.png", "123:64"),
        "state": IconSource(
            "file",
            "/usr/local/emhttp/state/plugins/dynamix.docker.manager/images/state.png",
            "456:128",
        ),
        "canonical": IconSource(
            "file", "/mnt/synthetic-cache-root/canonical.png", "789:256"
        ),
    }


def test_parse_icon_metadata_accepts_a_confirmed_empty_object():
    assert parse_icon_metadata("{}") == {}


@pytest.mark.parametrize("text", ["", "not-json", "[]", "null"])
def test_parse_icon_metadata_rejects_an_invalid_section(text):
    with pytest.raises((ValueError, TypeError)):
        parse_icon_metadata(text)


def test_select_icon_prefers_unraid_cache_then_uses_valid_label_url():
    container = Container(
        "web",
        "running",
        "example/web:latest",
        "stack",
        "web",
        "https://example.com/web.png",
    )
    cached = IconSource("file", "/var/lib/docker/unraid/images/web-icon.png", "123:64")

    assert select_icon(container, {"web": cached}) == cached
    assert select_icon(container, {}) == IconSource("url", "https://example.com/web.png", "")


def test_unraid_cache_also_works_without_a_compose_icon_label():
    container = Container("web", "exited", "example/web:latest", "", "")
    cached = IconSource("file", "/var/lib/docker/unraid/images/web-icon.png", "123:64")

    assert select_icon(container, {"web": cached}) == cached
    assert select_icon(container, {}) is None


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "https://" + "user:example-pass" + "@" + "example.com/web.png",
        "https://" + "user" + "@" + "example.com/web.png",
        "javascript:alert(1)",
        "file:///var/lib/docker/unraid/images/web.png",
        "https:///missing-host.png",
    ],
)
def test_select_icon_rejects_unsafe_or_incomplete_label_urls(value):
    container = Container("web", "running", "example/web:latest", "stack", "web", value)
    assert select_icon(container, {}) is None


@pytest.mark.parametrize("value", ["http://example.com/web.png", "https://example.com/web.png"])
def test_select_icon_accepts_http_urls_with_a_host(value):
    container = Container("web", "running", "example/web:latest", "stack", "web", value)
    assert select_icon(container, {}) == IconSource("url", value, "")


def test_unraid_question_mark_is_not_an_icon_source():
    text = '{"web":{"path":"/var/lib/docker/unraid/images/question.png","revision":"1:1"}}'
    assert parse_icon_metadata(text) == {}


def test_image_read_command_quotes_filename_and_checks_real_cache_boundary(tmp_path):
    import base64
    import shlex
    import subprocess
    from unraid_ssh import icons

    assert hasattr(icons, 'build_icon_read_command'), 'bounded image reader missing'
    root = tmp_path / 'cache'
    root.mkdir()
    safe = root / "a'; $(touch escaped).png"
    safe.write_bytes(b'picture')
    outside = tmp_path / 'private'
    outside.write_bytes(b'private')
    (root / 'escape.png').symlink_to(outside)
    (root / 'empty.png').touch()
    (root / 'large.png').write_bytes(b'x' * 1048577)
    for path, allowed in [(safe, True), (outside, False), (root / 'escape.png', False),
                          (root / 'empty.png', False), (root / 'large.png', False), (root, False)]:
        # Replace only fixed production root CLI arguments in this sandbox.
        argv = shlex.split(icons.build_icon_read_command(str(path)))
        argv[-2:] = [str(root), str(tmp_path / 'absent')]
        result = subprocess.run(shlex.join(argv), shell=True, capture_output=True, text=True, cwd=tmp_path)
        assert result.stderr == ''
        if allowed:
            assert result.returncode == 0
            assert base64.b64decode(result.stdout) == b'picture'
        else:
            assert result.returncode != 0 and result.stdout == ''
    assert not (tmp_path / 'escaped').exists()
