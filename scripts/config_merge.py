#!/usr/bin/env python3
"""Deep-merge new config keys into an existing config.yaml on update.

Single source of truth for the merge install.sh performs when updating an
existing install: the user's values are preserved and only keys introduced by a
newer version are injected (recursively for nested sections). install.sh calls
this module directly (`python3 scripts/config_merge.py <existing> <defaults>`),
and tests/test_config_merge.py locks the semantics below.

NOTE: like install.sh, this uses PyYAML's safe_load/dump, which does NOT
preserve comments. That is deliberate and unchanged — it is why install.sh also
writes an annotated config.yaml.dist reference next to the merged config.
"""
import sys

import yaml


def merge_new_keys(base: dict, new: dict) -> dict:
    """Return ``base`` with any keys from ``new`` it lacks (recursive for dicts).

    User values in ``base`` always win; only missing keys are added and no key is
    ever removed. Neither input is mutated.
    """
    result = dict(base)
    for k, v in new.items():
        if k not in result:
            result[k] = v
        elif isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = merge_new_keys(result[k], v)
    return result


def merge_files(existing_path: str, defaults_path: str) -> dict:
    """Merge new keys from ``defaults_path`` into ``existing_path``, writing it back.

    Returns the merged mapping. Mirrors install.sh's yaml.dump options so the
    on-disk output is identical.
    """
    with open(existing_path) as f:
        existing = yaml.safe_load(f) or {}
    with open(defaults_path) as f:
        defaults = yaml.safe_load(f) or {}
    merged = merge_new_keys(existing, defaults)
    with open(existing_path, "w") as f:
        yaml.dump(
            merged, f, default_flow_style=False, allow_unicode=True, sort_keys=False
        )
    return merged


if __name__ == "__main__":
    merge_files(sys.argv[1], sys.argv[2])
