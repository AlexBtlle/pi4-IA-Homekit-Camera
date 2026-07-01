"""Tests for the config deep-merge used on update (scripts/config_merge.py).

Locks the merge semantics that every future config key relies on, and checks
install.sh delegates to the module instead of carrying its own copy.
"""
from pathlib import Path

import yaml

from scripts.config_merge import merge_files, merge_new_keys

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "install.sh"


# ----------------------------------------------------------------------
# merge_new_keys — core semantics
# ----------------------------------------------------------------------

def test_preserves_user_value():
    # a value the user changed is never overwritten by the default
    assert merge_new_keys({"a": 1}, {"a": 2}) == {"a": 1}


def test_injects_new_key():
    # a key introduced by a newer version is added
    assert merge_new_keys({"a": 1}, {"a": 9, "b": 2}) == {"a": 1, "b": 2}


def test_keeps_user_only_key():
    # a key the user set but the defaults no longer ship is retained
    assert merge_new_keys({"a": 1, "x": 5}, {"a": 1}) == {"a": 1, "x": 5}


def test_nested_injects_subkey_and_preserves_user():
    base = {"camera": {"width": 256}}
    new = {"camera": {"width": 1920, "height": 1080}}
    assert merge_new_keys(base, new) == {"camera": {"width": 256, "height": 1080}}


def test_nested_does_not_clobber_section():
    # adding one sub-key must not wipe the rest of the user's section
    base = {"camera": {"width": 256, "user_tweak": 42}}
    new = {"camera": {"width": 1920}}
    assert merge_new_keys(base, new) == {"camera": {"width": 256, "user_tweak": 42}}


def test_scalar_vs_dict_mismatch_keeps_user():
    # user has a scalar where the defaults introduced a dict → keep the user value
    assert merge_new_keys({"a": 1}, {"a": {"x": 2}}) == {"a": 1}


def test_empty_base_gets_all_defaults():
    new = {"a": 1, "b": {"c": 2}}
    assert merge_new_keys({}, new) == {"a": 1, "b": {"c": 2}}


def test_does_not_mutate_inputs():
    base = {"camera": {"width": 256}}
    new = {"camera": {"width": 1920, "height": 1080}}
    merge_new_keys(base, new)
    assert base == {"camera": {"width": 256}}
    assert new == {"camera": {"width": 1920, "height": 1080}}


# ----------------------------------------------------------------------
# merge_files — round-trip through YAML on disk
# ----------------------------------------------------------------------

def test_merge_files_roundtrip(tmp_path):
    user = tmp_path / "config.yaml"
    defaults = tmp_path / "defaults.yaml"
    user.write_text("camera:\n  width: 256\n  min_area: 300\n")
    defaults.write_text(
        "camera:\n  width: 1920\n  min_area: 1500\n  snapshot_path: /dev/shm/x.jpg\n"
    )

    merged = merge_files(str(user), str(defaults))

    # user values preserved, new key injected
    assert merged["camera"]["width"] == 256
    assert merged["camera"]["min_area"] == 300
    assert merged["camera"]["snapshot_path"] == "/dev/shm/x.jpg"
    # and persisted back to the existing file
    assert yaml.safe_load(user.read_text()) == merged


# ----------------------------------------------------------------------
# wiring — install.sh must delegate to the module, not carry its own copy
# ----------------------------------------------------------------------

def test_install_sh_delegates_to_the_module():
    text = INSTALL_SH.read_text()
    # calls the shared module...
    assert "scripts/config_merge.py" in text
    # ...and does not reintroduce an inline copy of the merge logic
    assert "def merge_new_keys" not in text
