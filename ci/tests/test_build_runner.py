"""Run the orchestration in a temporary checkout with fake feeds/make, never firmware compilation."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

CI = Path(__file__).resolve().parents[1]
TARGET = "CONFIG_TARGET_DEVICE_mediatek_filogic_DEVICE_router"


class BuildRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        shutil.copytree(CI, self.root / "ci", ignore=shutil.ignore_patterns(
            "__pycache__", "tests", "presets", "configs", "assets", "feeds"))
        for name in ("presets", "scripts", "defconfig", "assets", "commands"):
            (self.root / name).mkdir()
        (self.root / "package").mkdir()
        (self.root / "ci/presets").mkdir(exist_ok=True)
        (self.root / "defconfig/base.config").write_text(
            f'CONFIG_TARGET_mediatek=y\nCONFIG_TARGET_mediatek_filogic=y\n{TARGET}=y\n'
            'CONFIG_TARGET_BOARD="mediatek"\nCONFIG_TARGET_SUBTARGET="filogic"\n')
        (self.root / "verified.config").write_text((self.root / "defconfig/base.config").read_text() +
                                                    'CONFIG_PACKAGE_verified-local=y\n')
        (self.root / "ci/devices.json").write_text(json.dumps({"router": {
            "defconfig": "defconfig/base.config", "config_symbol": TARGET, "target_dir": "mediatek/filogic",
            "image_glob": "*router-*", "required_images": ["sysupgrade", "initramfs"], "architecture": "aarch64"}}))
        (self.root / "feeds.conf.default").write_text("# fake feeds for orchestration tests\n")
        self.executable("scripts/feeds", '#!/bin/bash\nexit 0\n')
        self.executable("scripts/diffconfig.sh", '#!/bin/bash\ncat .config\n')
        self.executable("commands/git", '#!/bin/bash\nprintf "0000000000000000000000000000000000000001\\n"\n')
        self.executable("commands/make", '''#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == defconfig || "${1:-}" == download ]]; then exit 0; fi
[[ -f files/etc/chosen ]] && cp files/etc/chosen captured-rootfs
mkdir -p bin/targets/mediatek/filogic
printf 'image' > bin/targets/mediatek/filogic/router-sysupgrade.bin
printf 'image' > bin/targets/mediatek/filogic/router-initramfs.itb
''')
        self.environment = dict(os.environ, PATH=str(self.root / "commands") + os.pathsep + os.environ["PATH"],
                                CI_SKIP_FEEDS_UPDATE="1", CI_JOBS="1")

    def executable(self, path, contents):
        target = self.root / path
        target.write_text(contents)
        target.chmod(0o755)

    def preset(self, name="example", **changes):
        preset = {"name": name, "device": "router", "enabled": True, "config_file": "verified.config"}
        preset.update(changes)
        (self.root / f"ci/presets/{name}.json").write_text(json.dumps(preset))

    def run_build(self, name="example", **environment):
        return subprocess.run(["bash", "ci/build.sh", name], cwd=self.root,
                              env=dict(self.environment, **environment), text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def test_single_preset_cli_stages_files_and_keeps_checksums_valid_after_cleanup(self):
        (self.root / "assets/chosen").write_bytes(b"chosen profile")
        self.preset(files=[{"id": "chosen", "source": "assets/chosen", "destination": "/etc/chosen"}],
                    packages={"include": ["curl"], "exclude": []})
        result = self.run_build()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual((self.root / "captured-rootfs").read_bytes(), b"chosen profile")
        self.assertFalse((self.root / "files").exists())
        self.assertFalse((self.root / "package/ci-custom").exists())
        output = self.root / "output/example"
        self.assertIn('CONFIG_PACKAGE_curl=y', (output / "build.config").read_text())
        self.assertIn('CONFIG_PACKAGE_verified-local=y', (output / "build.config").read_text())
        self.assertTrue((output / "metadata/preset.json").is_file())
        for line in (output / "SHA256SUMS").read_text().splitlines():
            digest, filename = line.split("  ", 1)
            self.assertEqual(hashlib.sha256((output / filename).read_bytes()).hexdigest(), digest, filename)

    def test_disabled_preset_fails_before_touching_the_workspace(self):
        self.preset(enabled=False)
        result = self.run_build()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("disabled", result.stdout)
        self.assertFalse((self.root / ".config").exists())
        self.assertFalse((self.root / "output").exists())

    def test_configure_only_prepares_and_records_assets_without_compiling(self):
        (self.root / "assets/chosen").write_bytes(b"chosen profile")
        self.preset(files=[{"id": "chosen", "source": "assets/chosen", "destination": "/etc/chosen"}])
        result = self.run_build(CI_CONFIGURE_ONLY="1")
        self.assertEqual(result.returncode, 0, result.stdout)
        output = self.root / "output/example"
        metadata = json.loads((output / "metadata/rootfs-files.json").read_text())
        self.assertEqual(metadata["files"][0]["path"], "/etc/chosen")
        self.assertFalse((output / "SHA256SUMS").exists())
        self.assertFalse((self.root / "captured-rootfs").exists())

    def test_incremental_mode_records_inputs_before_owned_directories_are_removed(self):
        self.preset()
        result = self.run_build(CI_INCREMENTAL="1")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertTrue((self.root / ".ci-incremental/source-times.json").is_file())
        self.assertFalse((self.root / "package/ci-custom").exists())
        self.assertIn('# CONFIG_AUTOREMOVE is not set', (self.root / "output/example/build.config").read_text())

    def test_preset_feeds_are_installed_and_recorded(self):
        selected = "src-git packages https://example.com/packages.git^" + "a" * 40 + "\n"
        (self.root / "presets/feeds.conf").write_text(selected)
        self.preset(feeds_file="presets/feeds.conf")
        result = self.run_build(CI_CONFIGURE_ONLY="1")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual((self.root / "feeds.conf").read_text(), selected)
        self.assertEqual((self.root / "output/example/metadata/feeds.conf").read_text(), selected)
        # Re-running with the same selection also works.
        self.assertEqual(self.run_build(CI_CONFIGURE_ONLY="1").returncode, 0)

    def test_conflicting_local_feeds_fail_before_touching_the_workspace(self):
        (self.root / "presets/feeds.conf").write_text("# preset feeds\n")
        (self.root / "feeds.conf").write_text("# manually selected feeds\n")
        self.preset(feeds_file="presets/feeds.conf")
        result = self.run_build()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Existing feeds.conf differs", result.stdout)
        self.assertEqual((self.root / "feeds.conf").read_text(), "# manually selected feeds\n")
        self.assertFalse((self.root / ".config").exists())
        self.assertFalse((self.root / "output").exists())


if __name__ == "__main__":
    unittest.main()
