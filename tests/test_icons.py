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
