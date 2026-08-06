"""Guard: every key shipped in config.yaml must be read somewhere in the code.

Motivated by a real regression: camera.lux_path was read by the code but
missing from config.yaml (invisible to users, unreachable by install.sh's
deep-merge). This test pins the other direction too — a key present in the
shipped config but read nowhere is dead weight that misleads users.

The match is a word-boundary search across the Python and TypeScript sources:
config keys are always referenced by their literal name (cfg.get("x"),
camera.x ?? …), so a key whose name appears nowhere is genuinely unread.
Generic names (port, width…) match easily and weaken the guard for
themselves, but the failure mode this protects against — a NEW distinctive
key wired on one side only — is exactly where the guard bites.
"""
import pathlib
import re

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
SOURCE_GLOBS = [
    ("camera", "*.py"),
    ("homekit/src", "*.ts"),
    ("scripts", "*.py"),  # standalone consumers (IR-CUT daemon) count too
]


def _sources() -> str:
    chunks = []
    for folder, pattern in SOURCE_GLOBS:
        for path in (ROOT / folder).glob(pattern):
            chunks.append(path.read_text(encoding="utf-8"))
    return "\n".join(chunks)


def _leaf_keys(node, prefix=""):
    for key, value in node.items():
        dotted = f"{prefix}{key}"
        if isinstance(value, dict):
            yield from _leaf_keys(value, f"{dotted}.")
        else:
            yield key, dotted


def test_every_config_key_is_read_by_some_source():
    config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    sources = _sources()
    unread = [
        dotted
        for key, dotted in _leaf_keys(config)
        if not re.search(rf"\b{re.escape(key)}\b", sources)
    ]
    assert not unread, (
        f"config.yaml ships keys no source reads: {unread} — either wire them "
        f"or drop them (and if you just ADDED a code-side key, remember the "
        f"reverse rule: it must ship in config.yaml for the deep-merge)"
    )
