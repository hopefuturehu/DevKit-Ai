import os
from pathlib import Path

import pytest

from bot.sandbox.admission import inspect_roots
from bot.sandbox.models import SandboxError


def test_existing_hardlink_aliases_are_denied_without_opening_data(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    outside = tmp_path / "sentinel"
    outside.write_text("synthetic secret")
    alias = work / "alias"
    os.link(outside, alias)

    def unexpected(*a, **kw):
        pytest.fail("admission must not read file contents")

    monkeypatch.setattr(Path, "open", unexpected)
    admission = inspect_roots((work,))
    assert admission.hardlink_denies == (alias,)


@pytest.mark.parametrize("mutation", ["add", "replace", "rewrite", "link", "unlink", "symlink"])
def test_changed_snapshot_is_rejected_before_launch(tmp_path, mutation):
    path = tmp_path / "item"
    path.write_text("initial")
    admission = inspect_roots((tmp_path,))
    if mutation == "add":
        (tmp_path / "new").touch()
    elif mutation == "replace":
        temp = tmp_path / "replacement"
        temp.write_text("replacement")
        temp.replace(path)
    elif mutation == "rewrite":
        path.write_text("changed content")
    elif mutation == "link":
        os.link(path, tmp_path / "alias")
    elif mutation == "unlink":
        path.unlink()
    else:
        path.unlink()
        path.symlink_to("elsewhere")
    with pytest.raises(SandboxError, match="路径发生变化"):
        admission.verify()


def test_directory_changes_during_walk_cannot_replace_previous_stamp(tmp_path, monkeypatch):
    original = os.fwalk

    def changing_walk(*args, **kwargs):
        (tmp_path / "concurrent").touch()
        yield from original(*args, **kwargs)

    monkeypatch.setattr(os, "fwalk", changing_walk)
    with pytest.raises(SandboxError, match="路径发生变化"):
        inspect_roots((tmp_path,))


def test_limits_and_traversal_errors_do_not_become_empty_results(tmp_path, monkeypatch):
    (tmp_path / "file").touch()
    with pytest.raises(SandboxError, match="超过限制"):
        inspect_roots((tmp_path,), max_entries=1)
    with pytest.raises(SandboxError, match="超过限制"):
        inspect_roots((tmp_path,), timeout_seconds=-1)

    def unreadable(*a, **kw):
        raise PermissionError("synthetic inaccessible directory")

    monkeypatch.setattr(os, "fwalk", unreadable)
    with pytest.raises(SandboxError, match="inaccessible"):
        inspect_roots((tmp_path,))


def test_overlapping_roots_do_not_count_twice_and_symlinks_are_not_walked(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    child = work / "child"
    child.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "not-scanned").touch()
    (work / "link").symlink_to(outside, target_is_directory=True)
    admission = inspect_roots((work, child), max_entries=3)
    assert {stamp.path for stamp in admission.stamps} == {work, child, work / "link"}
    with pytest.raises(SandboxError, match="符号链接"):
        inspect_roots((work / "link",))


def test_excluded_control_tree_is_not_traversed(tmp_path):
    secret = tmp_path / "control"
    secret.mkdir()
    (secret / "credential").touch()
    admission = inspect_roots((tmp_path,), excluded=(secret,))
    assert [stamp.path for stamp in admission.stamps] == [tmp_path]
