#!/usr/bin/env python3
"""Prepare a single-device config and catch Kconfig silently dropping options."""

from pathlib import Path
import json
import os
import re
import sys

ASSIGNMENT = re.compile(r"(CONFIG_[A-Za-z0-9_-]+)=(.*)")
DISABLED = re.compile(r"# (CONFIG_[A-Za-z0-9_-]+) is not set")


def options(text):
    result = {}
    for line in text.splitlines():
        if match := ASSIGNMENT.fullmatch(line):
            result[match[1]] = match[2]
        elif match := DISABLED.fullmatch(line):
            result[match[1]] = "n"
    return result


def prepare(base, symbol, incremental=False):
    overrides = {
        "CONFIG_TARGET_MULTI_PROFILE": "y",
        "CONFIG_TARGET_ALL_PROFILES": "n",
        "CONFIG_TARGET_ROOTFS_SQUASHFS": "y",
        "CONFIG_TARGET_ROOTFS_INITRAMFS": "y",
        "CONFIG_DEVEL": "y",
        "CONFIG_CCACHE": "y",
        # Empty means $(TOPDIR)/.ccache in rules.mk. An explicit relative path
        # would resolve in each compiler's working directory instead.
        "CONFIG_CCACHE_DIR": '""',
        symbol: "y",
    }
    if incremental:
        overrides["CONFIG_AUTOREMOVE"] = "n"
    lines = []
    for line in base.splitlines():
        match = ASSIGNMENT.fullmatch(line) or DISABLED.fullmatch(line)
        key = match[1] if match else ""
        if key.startswith("CONFIG_TARGET_DEVICE_PACKAGES_") or key in overrides:
            continue
        if key.startswith("CONFIG_TARGET_DEVICE_"):
            line = f"# {key} is not set"
        lines.append(line)
    required = "\n".join(
        f"# {key} is not set" if value == "n" else f"{key}={value}"
        for key, value in overrides.items()
    ) + "\n"
    return "\n".join(lines) + "\n\n# CI requirements\n" + required, required


def validate(config, symbol, required):
    actual = options(config)
    selected = [key for key, value in actual.items()
                if key.startswith("CONFIG_TARGET_DEVICE_")
                and not key.startswith("CONFIG_TARGET_DEVICE_PACKAGES_") and value == "y"]
    errors = []
    if selected != [symbol]:
        errors.append(f"Expected only {symbol}; selected {selected}")
    for key, wanted in options(required).items():
        found = actual.get(key, "n")
        if found != wanted:
            errors.append(f"{key}: expected {wanted}, got {found}")
    if errors:
        raise ValueError("Kconfig requirements not satisfied:\n" + "\n".join(errors))


def apply_preset(config, required, preset, strict_packages=False):
    overrides = {}
    for action, value in (("include", "y"), ("exclude", "n")):
        for package in preset.get("packages", {}).get(action, []):
            overrides[f"CONFIG_PACKAGE_{package}"] = value
    for key, value in preset.get("kconfig", {}).items():
        if type(value) is bool:
            value = "y" if value else "n"
        elif isinstance(value, str):
            value = value if value in ("y", "n", "m") else json.dumps(value, ensure_ascii=False)
        else:
            value = str(value)
        overrides[key] = value
    mandatory = options(required)
    for key, value in overrides.items():
        if key in mandatory and mandatory[key] != value:
            raise ValueError(f"{key} conflicts with the selected device or feature requirements")
    lines = []
    for line in config.splitlines():
        match = ASSIGNMENT.fullmatch(line) or DISABLED.fullmatch(line)
        if not match or match[1] not in overrides:
            lines.append(line)
    additions = "\n".join(f"# {key} is not set" if value == "n" else f"{key}={value}"
                          for key, value in overrides.items())
    configured = "\n".join(lines) + "\n" + additions + "\n"
    requirements = required + additions + "\n"
    if strict_packages:
        # Apply deliberate preset overrides first, then require the remaining
        # imported packages so unavailable local packages cannot vanish silently.
        requirements += "\n".join(f"{key}={value}" for key, value in options(configured).items()
                                  if key.startswith("CONFIG_PACKAGE_") and value in ("y", "m")) + "\n"
    return configured, requirements


if __name__ == "__main__":
    try:
        command, filename, symbol, requirements = sys.argv[1:]
        if command == "prepare":
            config, required = prepare(Path(filename).read_text(), symbol, incremental=os.environ.get("CI_INCREMENTAL") == "1")
            Path(".config").write_text(config)
            Path(requirements).write_text(required)
        elif command == "validate":
            validate(Path(filename).read_text(), symbol, Path(requirements).read_text())
        elif command in ("apply", "apply-custom"):
            config, required = apply_preset(Path(filename).read_text(), Path(requirements).read_text(),
                                            json.loads(Path(symbol).read_text()), strict_packages=command == "apply-custom")
            Path(filename).write_text(config)
            Path(requirements).write_text(required)
        else:
            raise ValueError("Expected prepare, apply, apply-custom or validate")
    except (ValueError, OSError) as error:
        print(error, file=sys.stderr)
        sys.exit(1)
