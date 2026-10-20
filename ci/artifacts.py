#!/usr/bin/env python3
"""Collect one device's firmware and checksum the complete build artifact."""

import argparse
import fnmatch
import hashlib
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile


IMAGE_EXTENSIONS = (".bin", ".itb", ".img", ".ubi")
COMPRESSION_EXTENSIONS = ("", ".gz", ".xz", ".zst", ".bz2", ".lzma")
SHARED_METADATA = (
    "profiles.json",
    "config.buildinfo",
    "feeds.buildinfo",
    "version.buildinfo",
)


class ArtifactError(ValueError):
    """The selected build output cannot be safely published."""


def is_firmware(name):
    name = name.lower()
    return any(
        name.endswith(extension + compression)
        for extension in IMAGE_EXTENSIONS
        for compression in COMPRESSION_EXTENSIONS
    )


def _reject_symlink_components(path):
    for component in reversed((path, *path.parents)):
        if component.is_symlink():
            raise ArtifactError(f"symlink paths are not allowed: {component}")


def _require_regular_file(path):
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ArtifactError(f"expected a regular file, not a symlink or special file: {path}")


def _output_files(directory):
    """Walk without following symlinks; reject links instead of silently omitting them."""
    def fail(error):
        raise error

    result = []
    for current, directories, files in os.walk(directory, followlinks=False, onerror=fail):
        for name in directories:
            path = Path(current) / name
            if path.is_symlink():
                raise ArtifactError(f"symlink paths are not allowed: {path}")
        for name in files:
            path = Path(current) / name
            _require_regular_file(path)
            result.append(path)
    return sorted(result, key=lambda path: path.relative_to(directory).as_posix())


def _checksum_line(path, directory):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    name = path.relative_to(directory).as_posix()
    # Match GNU sha256sum's escaping so `sha256sum -c` handles unusual filenames.
    escaped = "\\" in name or "\n" in name
    name = name.replace("\\", "\\\\").replace("\n", "\\n")
    return ("\\" if escaped else "") + f"{digest.hexdigest()}  {name}\n"


def collect_artifacts(target_dir, image_glob, output_dir, required=()):
    """Copy selected images/metadata and return the copied basenames.

    ``required`` contains filename tokens such as ``sysupgrade`` or ``initramfs``.
    Each token must occur in at least one selected, nonempty firmware filename.
    Caller-supplied build metadata already in output_dir is included in SHA256SUMS.
    """
    if not image_glob or "/" in image_glob or "\\" in image_glob:
        raise ArtifactError("image glob must be a nonempty filename pattern without directories")
    target = Path(os.path.abspath(target_dir))
    output = Path(os.path.abspath(output_dir))
    _reject_symlink_components(target)
    _reject_symlink_components(output)
    if not target.is_dir():
        raise ArtifactError(f"target directory does not exist: {target}")
    if target == output or target in output.parents or output in target.parents:
        raise ArtifactError("target and output directories must not overlap")
    if output.exists() and not output.is_dir():
        raise ArtifactError(f"output path is not a directory: {output}")

    selected = []
    images = []
    for path in sorted(target.iterdir()):
        if not fnmatch.fnmatchcase(path.name, image_glob):
            continue
        firmware = is_firmware(path.name)
        if not firmware and not path.name.lower().endswith(".manifest"):
            continue
        _require_regular_file(path)
        if firmware:
            if path.stat().st_size == 0:
                continue
            images.append(path)
        selected.append(path)
    if not images:
        raise ArtifactError(f"no nonempty firmware images match {image_glob!r} in {target}")
    for token in required:
        if not token or not any(token.lower() in path.name.lower() for path in images):
            raise ArtifactError(f"required firmware image type is missing: {token!r}")

    for name in SHARED_METADATA:
        path = target / name
        if path.exists() or path.is_symlink():
            _require_regular_file(path)
            selected.append(path)

    # OpenWrt also emits a target-wide manifest without the device in its name.
    # Match both the target suffix and the distribution prefix of our images,
    # so manifests left by another device or distribution are not exported.
    manifest_suffix = f"-{target.parent.name}-{target.name}.manifest"
    for path in sorted(target.iterdir()):
        if (path.name.endswith(manifest_suffix)
                and any(image.name.startswith(path.stem + "-") for image in images)
                and path not in selected):
            _require_regular_file(path)
            selected.append(path)

    selected_names = {path.name for path in selected}
    if output.exists():
        for path in _output_files(output):
            if path.parent != output:
                continue
            relative = path.relative_to(output).as_posix()
            generated = (
                is_firmware(path.name)
                or path.name.lower().endswith(".manifest")
                or path.name in SHARED_METADATA
            )
            if generated and relative not in selected_names:
                raise ArtifactError(f"stale artifact in output directory; clean it before collecting: {path}")
    output.mkdir(parents=True, exist_ok=True)
    for path in selected:
        shutil.copyfile(path, output / path.name)

    # Compute before creating the temporary file and omit the old checksum file.
    lines = [
        _checksum_line(path, output)
        for path in _output_files(output)
        if path != output / "SHA256SUMS"
    ]
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", prefix=".SHA256SUMS.",
            dir=output, delete=False,
        ) as checksums:
            temporary = Path(checksums.name)
            checksums.writelines(lines)
        temporary.replace(output / "SHA256SUMS")
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return sorted(selected_names)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target_dir", help="one bin/targets/<target>/<subtarget> directory")
    parser.add_argument("image_glob", help="device filename pattern, for example '*ruijie_rg-x60-pro*'")
    parser.add_argument("output_dir", help="clean artifact directory, optionally containing build metadata")
    parser.add_argument(
        "--require", action="append", default=[], metavar="TYPE",
        help="require a firmware filename containing TYPE; may be repeated",
    )
    arguments = parser.parse_args(argv)
    try:
        copied = collect_artifacts(
            arguments.target_dir, arguments.image_glob, arguments.output_dir, arguments.require,
        )
    except (ArtifactError, OSError) as error:
        print(f"artifacts: {error}", file=sys.stderr)
        return 1
    print(f"Collected {len(copied)} files into {arguments.output_dir}; wrote SHA256SUMS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
