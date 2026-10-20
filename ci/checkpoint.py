#!/usr/bin/env python3
"""Persist complete OpenWrt build state and retain unchanged source timestamps."""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import time
from urllib.request import Request, urlopen

from config import options
from matrix import get_preset

ROOT = Path(__file__).resolve().parent.parent
STATE = ".ci-incremental"
SCHEMA = 1
# These contain both intermediate and installed outputs, not just ccache hits.
BUILD_ROOTS = ("build_dir", "staging_dir", "bin", "dl", ".ccache", "tmp", "feeds",
               "package/feeds", "target/linux/feeds", STATE)
ROOT_FILES = (".config", ".config.old", ".toolchain_build_ver", "feeds.conf",
              "private-key.pem", "public-key.pem")
ABI_PREFIXES = ("CONFIG_TARGET_", "CONFIG_ARCH_", "CONFIG_CPU_", "CONFIG_GCC_",
                "CONFIG_BINUTILS_", "CONFIG_LIBC_", "CONFIG_MUSL_", "CONFIG_GLIBC_",
                "CONFIG_UCLIBC_", "CONFIG_USE_MUSL", "CONFIG_USE_GLIBC", "CONFIG_USE_UCLIBC",
                "CONFIG_TOOLCHAIN_", "CONFIG_EXTERNAL_TOOLCHAIN", "CONFIG_NATIVE_TOOLCHAIN",
                "CONFIG_SOFT_FLOAT", "CONFIG_HAS_FPU")


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def git_files(root, *arguments):
    output = subprocess.check_output(["git", "ls-files", "-z", *arguments], cwd=root)
    return [os.fsdecode(name) for name in output.split(b"\0") if name]


def abi_options(values):
    return {key: value for key, value in values.items() if key.startswith(ABI_PREFIXES)
            and not key.startswith(("CONFIG_TARGET_DEVICE_", "CONFIG_TARGET_ROOTFS_"))}


def identity(root, name, environment=None):
    environment = os.environ if environment is None else environment
    preset = get_preset(name, root)
    device = json.loads((root / "ci/devices.json").read_text())[preset["device"]]
    base = root / preset.get("config_file", device["defconfig"])
    toolchain = {}
    board, subtarget = device["target_dir"].split("/")
    for filename in git_files(root, "--", "toolchain", "config/Config-toolchain.in", "rules.mk",
                              "config/Config-build.in", "include/toolchain-build.mk", "include/toolchain.mk",
                              "include/target.mk", f"target/linux/{board}/Makefile",
                              f"target/linux/{board}/{subtarget}/target.mk"):
        path = root / filename
        if path.is_file() and not path.is_symlink():
            toolchain[filename] = digest(path)
    feed_config = root / preset.get("feeds_file", "feeds.conf" if (root / "feeds.conf").is_file() else "feeds.conf.default")
    payload = {
        "schema": SCHEMA, "repository": environment.get("GITHUB_REPOSITORY", "local"),
        "ref": environment.get("GITHUB_REF", "local"), "preset": name,
        "device": device, "workspace": str(root.resolve()),
        "host": {"system": platform.system(), "arch": platform.machine(),
                 "os": Path("/etc/os-release").read_text(),
                 "image": environment.get("ImageOS", "local"),
                 "image_version": environment.get("ImageVersion", "local"),
                 "packages": hashlib.sha256(subprocess.check_output(
                     ["dpkg-query", "-W", "-f=${binary:Package}=${Version}\n"])).hexdigest(),
                 "gcc": subprocess.check_output(["gcc", "-dumpfullversion"], text=True).strip()},
        "toolchain_sources": toolchain, "feeds_config": digest(feed_config),
        "abi": abi_options(options(base.read_text())),
        "abi_overrides": abi_options(preset.get("kconfig", {})),
    }
    family = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:32]
    directory = root / STATE
    directory.mkdir(exist_ok=True)
    (directory / "identity.json").write_text(json.dumps({"family": family, "inputs": payload}, indent=2) + "\n")
    return family


def input_paths(root):
    paths = set(git_files(root))
    paths.add(".config")
    packages = root / "package/ci-custom"
    if packages.is_dir():
        paths.update(path.relative_to(root).as_posix() for path in packages.rglob("*")
                     if ".git" not in path.relative_to(packages).parts)
    return sorted(paths)


def capture(root):
    directory = root / STATE
    directory.mkdir(exist_ok=True)
    records = {}
    for filename in input_paths(root):
        path = root / filename
        if path.is_file() and not path.is_symlink():
            info = path.stat()
            records[filename] = {"sha256": digest(path), "mtime_ns": info.st_mtime_ns,
                                 "mode": stat.S_IMODE(info.st_mode)}
    (directory / "source-times.json").write_text(json.dumps(records, sort_keys=True) + "\n")
    packages = root / "package/ci-custom"
    if packages.is_dir():
        cached = directory / "custom-sources"
        shutil.rmtree(cached, ignore_errors=True)
        shutil.copytree(packages, cached, symlinks=True)
    return len(records)


def reuse_times(root):
    filename = root / STATE / "source-times.json"
    if not filename.is_file():
        return 0
    restored = 0
    for name, record in json.loads(filename.read_text()).items():
        relative = PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts or ".git" in relative.parts:
            raise ValueError("Invalid source timestamp path")
        path = root / name
        if path.is_file() and not path.is_symlink():
            info = path.stat()
            if stat.S_IMODE(info.st_mode) == record["mode"] and digest(path) == record["sha256"]:
                os.utime(path, ns=(info.st_atime_ns, record["mtime_ns"]))
                restored += 1
    return restored


def allowed_path(name):
    relative = PurePosixPath(name)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        return False
    value = relative.as_posix()
    return (any(value == base or value.startswith(base + "/") for base in BUILD_ROOTS)
            or value in ROOT_FILES or (len(relative.parts) == 1 and
                (value.startswith("key-build") or value.startswith("llvm-bpf")))
            or value.startswith("scripts/config/"))


def snapshot_paths(root):
    tracked = set(git_files(root))
    paths = [name for name in (*BUILD_ROOTS, *ROOT_FILES) if
             ((root / name).exists() or (root / name).is_symlink()) and name not in tracked]
    paths.extend(path.name for pattern in ("key-build*", "llvm-bpf*") for path in root.glob(pattern)
                 if path.name not in tracked and path.name not in paths)
    # Reuse generated Kconfig executables/objects without overwriting source files.
    paths.extend(git_files(root, "--others", "--ignored", "--exclude-standard", "--", "scripts/config"))
    return sorted(set(paths))


def pack(root, destination, family):
    identity_file = root / STATE / "identity.json"
    if not identity_file.is_file() or json.loads(identity_file.read_text())["family"] != family:
        raise ValueError("Checkpoint identity does not match this build")
    if not (root / STATE / "source-times.json").is_file():
        raise ValueError("Build has not recorded its source state; refusing an incomplete checkpoint")
    destination.mkdir(parents=True, exist_ok=True)
    archive = destination / "build-state.tar.zst"
    with tempfile.TemporaryFile() as listing:
        listing.write(b"\0".join(os.fsencode(name) for name in snapshot_paths(root)) + b"\0")
        listing.seek(0)
        with archive.open("wb") as output:
            tar = subprocess.Popen(["tar", "--format=posix", "--numeric-owner", "--null", "-T", "-", "-cf", "-"],
                                   stdin=listing, stdout=subprocess.PIPE, cwd=root)
            try:
                compressor = subprocess.run(["zstd", "-T0", "-3", "-c"], stdin=tar.stdout, stdout=output)
                tar.stdout.close()
                if compressor.returncode or tar.wait():
                    raise ValueError("Failed to archive the complete build state")
            finally:
                if tar.poll() is None:
                    tar.terminate()
                    tar.wait()
    manifest = {"schema": SCHEMA, "family": family, "sha256": digest(archive),
                "bytes": archive.stat().st_size, "created_at": int(time.time()),
                "roots": snapshot_paths(root)}
    (destination / "checkpoint.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return archive.stat().st_size


def verify_archive(archive):
    # Validate member names/links before GNU tar restores nanosecond mtimes.
    # Python's float-based extraction timestamps can change OpenWrt stamp hashes.
    process = subprocess.Popen(["zstd", "-dc", str(archive)], stdout=subprocess.PIPE)
    links = set()
    ancestors = set()
    try:
        with tarfile.open(fileobj=process.stdout, mode="r|") as incoming:
            for member in incoming:
                name = PurePosixPath(member.name).as_posix()
                if not allowed_path(name) or not (member.isdir() or member.isfile() or member.issym() or member.islnk()):
                    raise ValueError(f"Invalid checkpoint member: {member.name}")
                parents = {parent.as_posix() for parent in PurePosixPath(name).parents if parent.parts}
                if parents & links or (member.issym() and name in ancestors):
                    raise ValueError("Checkpoint entries cannot traverse symbolic links")
                ancestors.update(parents)
                if member.issym():
                    links.add(name)
                if member.islnk() and (not allowed_path(member.linkname) or
                        any(parent.as_posix() in links for parent in PurePosixPath(member.linkname).parents)
                        or member.linkname in links):
                    raise ValueError("Checkpoint hard links must stay inside regular build outputs")
        process.stdout.close()
        if process.wait():
            raise ValueError("Invalid compressed checkpoint")
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait()


def restore(root, directory, family):
    manifest = json.loads((directory / "checkpoint.json").read_text())
    archive = directory / "build-state.tar.zst"
    if manifest.get("schema") != SCHEMA or manifest.get("family") != family:
        raise ValueError("Checkpoint is incompatible with this preset, host, workspace or toolchain")
    if archive.stat().st_size != manifest["bytes"] or digest(archive) != manifest["sha256"]:
        raise ValueError("Checkpoint archive checksum/size mismatch")
    # Restoration is intended for the fresh checkout of a hosted runner.
    for base in BUILD_ROOTS:
        if base != STATE and ((root / base).exists() or (root / base).is_symlink()):
            raise ValueError(f"Refusing to overwrite existing build state: {base}")
    verify_archive(archive)
    command = ["tar", "--zstd", "-xf", str(archive), "-C", str(root), "--no-same-owner",
               "--no-wildcards", "--anchored"]
    command.extend("--exclude=" + name for name in git_files(root) if allowed_path(name))
    subprocess.run(command, check=True)
    return reuse_times(root)


def api_json(url, environment=None):
    environment = os.environ if environment is None else environment
    token = environment.get("GITHUB_TOKEN")
    if not token:
        raise ValueError("GITHUB_TOKEN is required to find earlier build checkpoints")
    request = Request(url, headers={"Authorization": f"Bearer {token}",
                      "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
    with urlopen(request, timeout=60) as response:
        return json.load(response)


def discover(prefix, repository):
    page = 1
    while True:
        items = api_json(f"https://api.github.com/repos/{repository}/actions/artifacts?per_page=100&page={page}")["artifacts"]
        for item in items:
            if (item["name"].startswith(prefix) and re.fullmatch(r"build-state-[a-f0-9]{32}-[0-9]+-[0-9]+", item["name"])
                    and not item.get("expired") and item.get("workflow_run")):
                return {"name": item["name"], "run_id": str(item["workflow_run"]["id"])}
        if len(items) < 100:
            return {"name": "", "run_id": ""}
        page += 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("identity", "capture", "reuse-times", "pack", "restore", "discover"))
    parser.add_argument("values", nargs="*")
    args = parser.parse_args()
    try:
        if args.command == "identity":
            family = identity(ROOT, args.values[0])
            print(f"family={family}\nprefix=build-state-{family}-")
        elif args.command == "capture":
            print(f"Recorded {capture(ROOT)} source timestamps")
        elif args.command == "reuse-times":
            print(f"Reused {reuse_times(ROOT)} unchanged source timestamps")
        elif args.command == "pack":
            print(f"Saved {pack(ROOT, Path(args.values[0]), args.values[1])} bytes of complete build state")
        elif args.command == "restore":
            print(f"Restored complete build state and {restore(ROOT, Path(args.values[0]), args.values[1])} source timestamps")
        elif args.command == "discover":
            for key, value in discover(args.values[0], os.environ["GITHUB_REPOSITORY"]).items():
                print(f"{key}={value}")
    except (ValueError, OSError, KeyError, IndexError, subprocess.CalledProcessError, tarfile.TarError) as error:
        parser.exit(1, f"Build checkpoint: {error}\n")


if __name__ == "__main__":
    main()
