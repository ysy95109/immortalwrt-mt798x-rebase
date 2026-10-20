#!/usr/bin/env python3
"""Assemble the selected preset's rootfs overlay and record every included file."""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import struct
import subprocess
import tarfile
import tempfile
from urllib.parse import quote

from matrix import checked_path, https_url, image_path


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(url, destination, api=False):
    https_url(url)
    command = ["curl", "--fail", "--silent", "--show-error", "--location", "--retry", "3",
               "--connect-timeout", "20", "--max-time", "300", "--proto", "=https",
               "--proto-redir", "=https", "--output", str(destination)]
    if api:
        command += ["-H", "Accept: application/vnd.github+json", "-H", "X-GitHub-Api-Version: 2022-11-28"]
        if token := os.environ.get("GITHUB_TOKEN"):
            command += ["-H", f"Authorization: Bearer {token}"]
    # Do not echo command arguments: authenticated API calls contain a token.
    result = subprocess.run(command + [url], check=False)
    if result.returncode:
        raise ValueError(f"Download failed (curl exit {result.returncode}): {url}")


def github_release(source, work):
    repository = source["repository"]
    base = f"https://api.github.com/repos/{repository}/releases"
    ref = source.get("ref", "latest")
    metadata = work / "release.json"
    if ref == "latest":
        # Paginate: /latest omits prereleases, and the first page alone need not
        # contain the most recently published release.
        releases = []
        page = 1
        while True:
            download(f"{base}?per_page=100&page={page}", metadata, api=True)
            items = json.loads(metadata.read_text())
            if not isinstance(items, list):
                raise ValueError("GitHub release list is not an array")
            releases.extend(item for item in items if not item.get("draft") and
                            (source.get("prereleases", False) or not item.get("prerelease")))
            if len(items) < 100:
                break
            page += 1
        if not releases:
            raise ValueError(f"No matching published releases for {repository}")
        release = max(releases, key=lambda item: item.get("published_at") or item.get("created_at") or "")
    else:
        download(f"{base}/tags/{quote(ref, safe='')}", metadata, api=True)
        release = json.loads(metadata.read_text())
        if release.get("draft"):
            raise ValueError("Cannot use a draft release")
    tag = release["tag_name"]
    filename = source["asset"].format(tag=tag, version=tag.removeprefix("v"))
    assets = [item for item in release.get("assets", []) if item.get("state") == "uploaded"]
    matching = [item for item in assets if item.get("name") == filename]
    if len(matching) != 1:
        raise ValueError(f"Expected one release asset {filename!r} in {repository}@{tag}")
    asset = matching[0]
    prefix = f"https://github.com/{repository}/releases/download/"
    if not asset["browser_download_url"].startswith(prefix):
        raise ValueError("Unexpected GitHub release asset URL")
    return release, asset, assets, prefix


def get_source(entry, root, work):
    record = {"id": entry["id"], "destination": entry["destination"]}
    if "source" in entry:
        path = root / entry["source"]
        checked_path(root, entry["source"], entry["id"], directory=path.is_dir())
        record["source"] = entry["source"]
        expected = entry.get("sha256")
    else:
        path = work / "download"
        expected = entry.get("sha256")
        if "github" in entry:
            release, asset, assets, prefix = github_release(entry["github"], work)
            url = asset["browser_download_url"]
            record.update(repository=entry["github"]["repository"], tag=release["tag_name"],
                          prerelease=release.get("prerelease", False), asset=asset["name"])
            if not expected:
                digest = asset.get("digest")
                if isinstance(digest, str) and re.fullmatch(r"sha256:[a-fA-F0-9]{64}", digest):
                    expected = digest[7:]
                elif not digest:
                    sums = [item for item in assets if item.get("name") == "SHA256SUMS"]
                    if len(sums) != 1 or not sums[0]["browser_download_url"].startswith(prefix):
                        raise ValueError("Release asset needs a SHA256 digest or SHA256SUMS")
                    download(sums[0]["browser_download_url"], work / "SHA256SUMS")
                    matches = []
                    for line in (work / "SHA256SUMS").read_text().splitlines():
                        match = re.fullmatch(r"([a-fA-F0-9]{64}) [ *](.+)", line)
                        if match and match[2] in (asset["name"], "./" + asset["name"]):
                            matches.append(match[1])
                    if len(matches) != 1:
                        raise ValueError("Expected one SHA256SUMS entry for the release asset")
                    expected = matches[0]
                else:
                    raise ValueError("Malformed or unsupported release asset digest")
        else:
            url = entry["url"]
        download(url, path)
        record["url"] = url
        if not expected:
            raise ValueError("Downloaded files require a SHA256 checksum")
    if path.is_file():
        actual = sha256(path)
        if expected and actual != expected.lower():
            raise ValueError(f"SHA256 mismatch for {entry['id']}")
        record["sha256"] = actual
    elif expected:
        raise ValueError("A directory cannot have an archive/file SHA256 checksum")
    return path, record


def extract_tar(source, destination, strip_components=0):
    destination.mkdir()
    with tarfile.open(source, "r:*") as archive:
        members = []
        names = set()
        total = 0
        for member in archive.getmembers():
            original = PurePosixPath(member.name)
            if original.is_absolute() or ".." in original.parts or "\\" in member.name:
                raise ValueError(f"Unsafe archive path: {member.name!r}")
            if not (member.isdir() or member.isfile()):
                raise ValueError(f"Archive links and special files are unsupported: {member.name!r}")
            parts = original.parts[strip_components:]
            if not parts:
                if member.isdir():
                    continue
                raise ValueError("strip_components removes an entire file path")
            relative = Path(*parts)
            if relative in names:
                raise ValueError(f"Duplicate archive path after stripping: {relative}")
            names.add(relative)
            total += member.size
            if total > 512 * 1024 * 1024:
                raise ValueError("Archive exceeds the 512 MiB extraction limit")
            members.append((member, relative))
        for member, relative in members:
            target = destination / relative
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                target.chmod(0o755)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as incoming, target.open("xb") as output:
                    shutil.copyfileobj(incoming, output)
                target.chmod(0o755 if member.mode & 0o111 else 0o644)


def regular_files(directory):
    for path in sorted(directory.rglob("*")):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ValueError(f"Overlay links and special files are unsupported: {path}")
        if path.is_file():
            yield path


def copy_file(source, destination, replace=False, mode=None):
    if source.is_symlink() or not stat.S_ISREG(source.stat().st_mode):
        raise ValueError(f"Expected a regular source file: {source}")
    if destination.exists() and not replace:
        raise ValueError(f"File already supplied by an overlay or asset: {destination}; set replace=true to replace it")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    destination.chmod(int(mode, 8) if mode else source.stat().st_mode & 0o777)


def merge_tree(source, destination, replace=False):
    destination.mkdir(parents=True, exist_ok=True)
    for path in sorted(source.rglob("*")):
        target = destination / path.relative_to(source)
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ValueError(f"Overlay links and special files are unsupported: {path}")
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            copy_file(path, target, replace=replace)


def check_elf(path, architecture):
    machines = {"aarch64": (183, 2), "arm": (40, 1), "x86_64": (62, 2), "riscv64": (243, 2)}
    with path.open("rb") as binary:
        header = binary.read(20)
    if len(header) < 20 or header[:4] != b"\x7fELF" or header[5] not in (1, 2):
        raise ValueError(f"Expected an ELF binary for {architecture}: {path.name}")
    machine = struct.unpack("<H" if header[5] == 1 else ">H", header[18:20])[0]
    if (machine, header[4]) != machines[architecture]:
        raise ValueError(f"ELF architecture mismatch, expected {architecture}: {path.name}")


def assemble(preset, root, destination, metadata):
    # This destination belongs to this invocation. Never reuse an old overlay.
    if destination.is_symlink() or (destination.exists() and any(destination.iterdir())):
        raise ValueError("Rootfs staging directory must be empty")
    destination.mkdir(parents=True, exist_ok=True)
    records = []
    for overlay in preset.get("overlays", []):
        source = checked_path(root, overlay, "overlay", directory=True)
        merge_tree(source, destination, replace=True)
    with tempfile.TemporaryDirectory(prefix="rootfs-", dir=metadata.parent) as temporary:
        for index, entry in enumerate(preset.get("files", [])):
            work = Path(temporary) / str(index)
            work.mkdir()
            source, record = get_source(entry, root, work)
            target = destination.joinpath(*PurePosixPath(entry["destination"]).parts[1:])
            tree = source
            if entry.get("archive"):
                tree = work / "unpacked"
                extract_tar(source, tree, entry.get("strip_components", 0))
            if tree.is_dir():
                for required in entry.get("required_files", []):
                    image_path(required, "required file", absolute=False)
                    if not (tree / required).is_file():
                        raise ValueError(f"{entry['id']}: missing required file {required}")
                merge_tree(tree, target, replace=entry.get("replace", False))
            else:
                if entry.get("architecture"):
                    check_elf(tree, entry["architecture"])
                copy_file(tree, target, replace=entry.get("replace", False), mode=entry.get("mode"))
            records.append(record)
    destination.chmod(0o755)
    for path in destination.rglob("*"):
        if path.is_dir():
            path.chmod(0o755)
    inventory = [{"path": "/" + path.relative_to(destination).as_posix(), "sha256": sha256(path),
                  "mode": f"{stat.S_IMODE(path.stat().st_mode):04o}"} for path in regular_files(destination)]
    metadata.write_text(json.dumps({"overlays": preset.get("overlays", []), "sources": records,
                                    "files": inventory}, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("preset", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("metadata", type=Path)
    args = parser.parse_args()
    try:
        assemble(json.loads(args.preset.read_text()), Path(__file__).resolve().parent.parent,
                 args.destination, args.metadata)
    except (ValueError, OSError, KeyError, tarfile.TarError) as error:
        parser.exit(1, f"Extra files: {error}\n")
