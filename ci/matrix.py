#!/usr/bin/env python3
"""Validate independent build presets and emit a matrix or one resolved preset."""

import argparse
import json
from pathlib import Path, PurePosixPath
import re
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}\Z")
SYMBOL = re.compile(r"CONFIG_TARGET_DEVICE_[A-Za-z0-9_-]+\Z")
CONFIG = re.compile(r"CONFIG_[A-Za-z0-9_-]+\Z")
PACKAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]*\Z")
ARCHITECTURES = {"aarch64", "arm", "x86_64", "riscv64"}


def checked_path(root, value, label, directory=False):
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a nonempty repository path")
    path = root / value
    if Path(value).is_absolute() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"{label} must stay inside the repository (relative path required)")
    if not (path.is_dir() if directory else path.is_file()):
        raise ValueError(f"{label} not found: {value}")
    return path


def string_list(value, label, pattern=NAME):
    if not isinstance(value, list) or any(not isinstance(item, str) or
            not pattern.fullmatch(item) for item in value) or len(value) != len(set(value)):
        raise ValueError(f"{label} must be a list of unique valid names")


def image_path(value, label, absolute=True):
    if not isinstance(value, str) or not value or "\\" in value or "\n" in value or "\x00" in value:
        raise ValueError(f"Invalid {label}")
    path = PurePosixPath(value)
    if path.is_absolute() != absolute or ".." in path.parts or (not absolute and not path.parts):
        raise ValueError(f"Invalid {label}: {value!r}")


def https_url(value):
    if not isinstance(value, str):
        raise ValueError("Download URL must be HTTPS")
    url = urlsplit(value)
    if url.scheme != "https" or not url.hostname or url.username or url.password or url.fragment:
        raise ValueError("Download URL must be HTTPS without credentials or a fragment")


def validate_files(files, root, device):
    if not isinstance(files, list):
        raise ValueError("files must be a list")
    identifiers = set()
    for entry in files:
        allowed = {"id", "source", "url", "github", "destination", "sha256", "mode",
                   "archive", "strip_components", "required_files", "architecture", "replace"}
        if not isinstance(entry, dict) or set(entry) - allowed:
            raise ValueError("Invalid or unknown extra-file fields")
        identifier = entry.get("id", "")
        if not isinstance(identifier, str) or not NAME.fullmatch(identifier) or identifier in identifiers:
            raise ValueError("Every extra file needs a unique valid id")
        identifiers.add(identifier)
        if sum(key in entry for key in ("source", "url", "github")) != 1:
            raise ValueError(f"{identifier}: choose exactly one of source, url or github")
        if "source" in entry:
            source = root / entry["source"] if isinstance(entry["source"], str) else root
            checked_path(root, entry["source"], identifier, directory=source.is_dir())
        is_tree = bool(entry.get("archive")) or ("source" in entry and source.is_dir())
        if is_tree and any(key in entry for key in ("mode", "architecture")):
            raise ValueError(f"{identifier}: mode and architecture apply to individual raw files")
        if "source" in entry and source.is_dir() and "sha256" in entry:
            raise ValueError(f"{identifier}: directory checksums are recorded per file, not as one sha256")
        if not is_tree and entry.get("required_files"):
            raise ValueError(f"{identifier}: required_files needs a directory or archive")
        if "url" in entry:
            https_url(entry["url"])
            if "sha256" not in entry:
                raise ValueError(f"{identifier}: URL downloads require sha256")
        if "sha256" in entry and (not isinstance(entry["sha256"], str) or
                not re.fullmatch(r"[a-fA-F0-9]{64}", entry["sha256"])):
            raise ValueError(f"{identifier}: invalid sha256")
        if "github" in entry:
            github = entry["github"]
            if not isinstance(github, dict) or set(github) - {"repository", "ref", "prereleases", "asset"}:
                raise ValueError(f"{identifier}: invalid github release source")
            if not isinstance(github.get("repository"), str) or not re.fullmatch(
                    r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", github["repository"]):
                raise ValueError(f"{identifier}: invalid GitHub repository")
            if not isinstance(github.get("ref", "latest"), str) or not github.get("ref", "latest"):
                raise ValueError(f"{identifier}: invalid GitHub release ref")
            if type(github.get("prereleases", False)) is not bool:
                raise ValueError(f"{identifier}: prereleases must be boolean")
            asset = github.get("asset")
            if not isinstance(asset, str) or not asset or "/" in asset or "\\" in asset:
                raise ValueError(f"{identifier}: invalid GitHub asset basename")
            try:
                asset.format(tag="v1", version="1")
            except (KeyError, ValueError, IndexError, AttributeError):
                raise ValueError(f"{identifier}: asset only supports {{tag}} and {{version}}") from None
        image_path(entry.get("destination"), f"{identifier} destination")
        if entry["destination"] == "/" and not is_tree:
            raise ValueError(f"{identifier}: a raw file needs a destination filename")
        if "mode" in entry and (not isinstance(entry["mode"], str) or
                not re.fullmatch(r"0[0-7]{3}", entry["mode"])):
            raise ValueError(f"{identifier}: mode must be an octal string such as 0755")
        if entry.get("archive") not in (None, "tar"):
            raise ValueError(f"{identifier}: archive must be tar")
        strip = entry.get("strip_components", 0)
        if type(strip) is not int or strip < 0 or (strip and not entry.get("archive")):
            raise ValueError(f"{identifier}: strip_components needs a tar archive and a nonnegative integer")
        required = entry.get("required_files", [])
        if not isinstance(required, list):
            raise ValueError(f"{identifier}: required_files must be a list")
        for path in required:
            image_path(path, f"{identifier} required file", absolute=False)
        if "architecture" in entry and (entry["architecture"] not in ARCHITECTURES or
                entry["architecture"] != device.get("architecture")):
            raise ValueError(f"{identifier}: binary architecture does not match the device")
        if type(entry.get("replace", False)) is not bool:
            raise ValueError(f"{identifier}: replace must be boolean")


def load_catalog(root=ROOT):
    with (root / "ci/devices.json").open() as stream:
        devices = json.load(stream)
    if not isinstance(devices, dict) or not devices:
        raise ValueError("ci/devices.json must contain a nonempty object")
    for name, device in devices.items():
        if not NAME.fullmatch(name) or not isinstance(device, dict):
            raise ValueError(f"Invalid device: {name!r}")
        checked_path(root, device.get("defconfig"), f"{name}.defconfig")
        if not isinstance(device.get("config_symbol"), str) or not SYMBOL.fullmatch(device["config_symbol"]):
            raise ValueError(f"Invalid config_symbol for {name}")
        if not isinstance(device.get("target_dir"), str) or not re.fullmatch(
                r"[a-z0-9_-]+/[a-z0-9_-]+", device["target_dir"]):
            raise ValueError(f"Invalid target_dir for {name}")
        pattern = device.get("image_glob", "")
        if not isinstance(pattern, str) or not pattern or re.search(r"[/\\\s]", pattern):
            raise ValueError(f"image_glob must be a basename pattern for {name}")
        string_list(device.get("required_images", []), f"{name}.required_images")
        if "architecture" in device and device["architecture"] not in ARCHITECTURES:
            raise ValueError(f"Unsupported architecture for {name}")
    builds = []
    names = set()
    for filename in sorted((root / "ci/presets").glob("*.json")):
        build = json.loads(filename.read_text())
        fields = {"name", "enabled", "description", "requirements", "device", "config_file",
                  "features", "feature_options", "packages", "kconfig", "overlays", "files"}
        if not isinstance(build, dict) or set(build) - fields:
            raise ValueError(f"Unknown preset fields in {filename.name}")
        name = build.get("name", "")
        if not isinstance(name, str) or not NAME.fullmatch(name) or name == "all" or name in names:
            raise ValueError(f"Invalid or duplicate preset: {name!r}")
        if filename.stem != name:
            raise ValueError(f"Preset filename must match its name: {name}")
        names.add(name)
        if type(build.setdefault("enabled", True)) is not bool:
            raise ValueError(f"{name}: enabled must be boolean")
        if "requirements" in build and not isinstance(build["requirements"], dict):
            raise ValueError(f"{name}: requirements must be an object")
        if not isinstance(build.get("device"), str) or build["device"] not in devices:
            raise ValueError(f"Unknown device in {name}")
        if "config_file" in build:
            checked_path(root, build["config_file"], f"{name}.config_file")
        build.setdefault("features", [])
        string_list(build["features"], f"{name}.features")
        for feature in build["features"]:
            checked_path(root, f"ci/features/{feature}.sh", f"{name} feature")
        options = build.setdefault("feature_options", {})
        if not isinstance(options, dict) or set(options) - set(build["features"]) or any(
                not isinstance(value, dict) for value in options.values()):
            raise ValueError(f"{name}: feature_options must refer to selected features")
        packages = build.setdefault("packages", {})
        if not isinstance(packages, dict) or set(packages) - {"include", "exclude"}:
            raise ValueError(f"{name}: invalid packages")
        for key in ("include", "exclude"):
            string_list(packages.setdefault(key, []), f"{name}.packages.{key}", PACKAGE)
        if set(packages["include"]) & set(packages["exclude"]):
            raise ValueError(f"{name}: a package cannot be included and excluded together")
        kconfig = build.setdefault("kconfig", {})
        if not isinstance(kconfig, dict) or any(not CONFIG.fullmatch(key) or
                type(value) not in (bool, int, str) or (isinstance(value, str) and
                any(char in value for char in ("\n", "\r", "\x00"))) for key, value in kconfig.items()):
            raise ValueError(f"{name}: invalid kconfig values")
        if any(key.startswith(("CONFIG_TARGET_", "CONFIG_PACKAGE_")) or key == "CONFIG_CCACHE_DIR" for key in kconfig):
            raise ValueError(f"{name}: select devices/packages in their dedicated fields")
        overlays = build.setdefault("overlays", [])
        if not isinstance(overlays, list):
            raise ValueError(f"{name}: overlays must be a list")
        for overlay in overlays:
            checked_path(root, overlay, f"{name} overlay", directory=True)
        validate_files(build.setdefault("files", []), root, devices[build["device"]])
        builds.append(build)
    if len(builds) > 256:
        raise ValueError("ci/presets/ can contain at most 256 presets")
    return builds


def get_preset(name, root=ROOT):
    for build in load_catalog(root):
        if build["name"] == name:
            return build
    raise ValueError(f"Unknown preset: {name}")


def matrix(preset, root=ROOT):
    builds = load_catalog(root)
    if preset != "all":
        matching = [build for build in builds if build["name"] == preset]
        if not matching:
            raise ValueError(f"Unknown preset: {preset}")
        if not matching[0]["enabled"]:
            raise ValueError(f"Preset {preset} is disabled; import the verified local configuration before enabling")
    selected = [build for build in builds if build["enabled"] and (preset == "all" or build["name"] == preset)]
    if not selected:
        raise ValueError("No enabled presets; import the verified local configurations before enabling builds")
    return {"include": [{"name": build["name"], "device": build["device"]} for build in selected]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("preset", nargs="?", help="preset name, or all for a matrix")
    parser.add_argument("--preset", action="store_true", dest="full", help="print the complete resolved preset")
    parser.add_argument("--check", action="store_true", help="validate available presets without starting a build")
    args = parser.parse_args()
    try:
        if args.check:
            print(f"Validated {len(load_catalog())} preset(s)")
        elif args.preset:
            print(json.dumps(get_preset(args.preset) if args.full else matrix(args.preset), separators=(",", ":")))
        else:
            parser.error("provide a preset name or --check")
    except (ValueError, OSError, TypeError) as error:
        print(f"Build catalog error: {error}", file=sys.stderr)
        sys.exit(1)
