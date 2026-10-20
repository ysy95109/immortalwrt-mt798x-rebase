"""Full-state round trips and real make dependency checks, using tiny local builds."""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

CI = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CI))
spec = importlib.util.spec_from_file_location("checkpoint_under_test", CI / "checkpoint.py")
checkpoint = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checkpoint)


@unittest.skipUnless(shutil.which("zstd") and shutil.which("gcc"), "zstd and gcc required")
class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.parent = Path(self.temporary.name)
        self.root = self.parent / "checkout"
        self.root.mkdir()
        for name in ("ci/presets", "defconfig", "src", "scripts/config", "toolchain"):
            (self.root / name).mkdir(parents=True)
        (self.root / "src/main.c").write_text("int value(void) { return 1; }\n")
        (self.root / "src/change.c").write_text("original\n")
        (self.root / "toolchain/Makefile").write_text("# toolchain\n")
        (self.root / "scripts/config/Makefile").write_text("# kconfig source\n")
        (self.root / "Makefile").write_text("build_dir/main.o: src/main.c\n\tmkdir -p build_dir\n\tgcc -c $< -o $@\n")
        (self.root / ".gitignore").write_text("/build_dir\n/staging_dir\n/feeds\n/bin\n/.ci-incremental\n/.config\n*.o\n/scripts/config/conf\n")
        (self.root / "feeds.conf.default").write_text("# test feeds\n")
        (self.root / "defconfig/router.config").write_text("CONFIG_TARGET_mediatek=y\nCONFIG_GCC_VERSION=\"14.3.0\"\n")
        (self.root / "ci/devices.json").write_text(json.dumps({"router": {
            "defconfig": "defconfig/router.config", "config_symbol": "CONFIG_TARGET_DEVICE_mediatek_filogic_DEVICE_router",
            "target_dir": "mediatek/filogic", "image_glob": "*router*"}}))
        (self.root / "ci/presets/example.json").write_text(json.dumps({"name": "example", "device": "router"}))
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(["git", "add", "."], cwd=self.root, check=True)
        self.environment = {"GITHUB_REPOSITORY": "owner/repo", "GITHUB_REF": "refs/heads/build",
                            "ImageOS": "ubuntu24", "ImageVersion": "20261020.1"}
        self.family = checkpoint.identity(self.root, "example", self.environment)
        self.archive = self.parent / "archive"

    def prepare_state(self):
        for name in ("build_dir", "staging_dir/host/bin", "feeds/packages", "bin/packages"):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        (self.root / ".config").write_text("CONFIG_TARGET_mediatek=y\n")
        subprocess.run(["make"], cwd=self.root, check=True, stdout=subprocess.DEVNULL)
        source = self.root / "feeds/packages/Makefile"
        source.write_text("# package source\n")
        os.utime(source, ns=(1700000000123456789, 1700000000123456789))
        (self.root / "bin/packages/test.ipk").write_bytes(b"compiled package")
        script = self.root / "staging_dir/host/bin/tool"
        script.write_text("#!/bin/sh\nexit 0\n")
        script.chmod(0o755)
        (self.root / "staging_dir/host/bin/tool-link").symlink_to(script)
        os.link(self.root / "build_dir/main.o", self.root / "build_dir/alias.o")
        (self.root / "scripts/config/conf").write_bytes(b"compiled kconfig")
        checkpoint.capture(self.root)
        checkpoint.pack(self.root, self.archive, self.family)

    def discard_build_state(self):
        for name in checkpoint.BUILD_ROOTS:
            path = self.root / name
            if path.is_symlink():
                path.unlink()
            elif path.is_dir():
                shutil.rmtree(path)
        (self.root / "scripts/config/conf").unlink(missing_ok=True)

    def test_full_roundtrip_preserves_artifacts_links_modes_and_nanosecond_timestamps(self):
        self.prepare_state()
        self.discard_build_state()
        checkpoint.restore(self.root, self.archive, self.family)
        self.assertEqual((self.root / "bin/packages/test.ipk").read_bytes(), b"compiled package")
        self.assertEqual((self.root / "feeds/packages/Makefile").stat().st_mtime_ns, 1700000000123456789)
        self.assertTrue(os.access(self.root / "staging_dir/host/bin/tool", os.X_OK))
        self.assertTrue((self.root / "staging_dir/host/bin/tool-link").is_symlink())
        self.assertEqual((self.root / "build_dir/main.o").stat().st_ino,
                         (self.root / "build_dir/alias.o").stat().st_ino)
        self.assertEqual((self.root / "scripts/config/conf").read_bytes(), b"compiled kconfig")

    def test_restoring_a_fresh_checkout_really_skips_unchanged_compilation(self):
        self.prepare_state()
        compiled_at = (self.root / "build_dir/main.o").stat().st_mtime_ns
        self.discard_build_state()
        # A normal actions/checkout gives all tracked files new timestamps.
        source = self.root / "src/main.c"
        source.write_text(source.read_text())
        os.utime(source, ns=(compiled_at + 2_000_000_000, compiled_at + 2_000_000_000))
        checkpoint.restore(self.root, self.archive, self.family)
        result = subprocess.run(["make", "-q"], cwd=self.root)
        self.assertEqual(result.returncode, 0, "unchanged source should reuse the actual object file")
        self.assertEqual((self.root / "build_dir/main.o").stat().st_mtime_ns, compiled_at)

    def test_changed_sources_are_kept_and_trigger_a_real_rebuild(self):
        self.prepare_state()
        self.discard_build_state()
        source = self.root / "src/main.c"
        source.write_text("int value(void) { return 2; }\n")
        checkpoint.restore(self.root, self.archive, self.family)
        self.assertIn("return 2", source.read_text())
        self.assertEqual(subprocess.run(["make", "-q"], cwd=self.root).returncode, 1)
        subprocess.run(["make"], cwd=self.root, check=True, stdout=subprocess.DEVNULL)
        self.assertEqual(subprocess.run(["make", "-q"], cwd=self.root).returncode, 0)

    def test_incompatible_family_and_corrupt_archives_are_rejected(self):
        self.prepare_state()
        self.discard_build_state()
        with self.assertRaisesRegex(ValueError, "incompatible"):
            checkpoint.restore(self.root, self.archive, "different-family")
        with (self.archive / "build-state.tar.zst").open("ab") as stream:
            stream.write(b"corrupt")
        with self.assertRaisesRegex(ValueError, "checksum/size"):
            checkpoint.restore(self.root, self.archive, self.family)

    def test_package_and_overlay_changes_keep_the_family_but_host_and_toolchain_changes_do_not(self):
        preset = self.root / "ci/presets/example.json"
        preset.write_text(json.dumps({"name": "example", "device": "router", "packages": {"include": ["curl"]}}))
        self.assertEqual(checkpoint.identity(self.root, "example", self.environment), self.family)
        self.assertNotEqual(checkpoint.identity(self.root, "example", dict(self.environment, ImageVersion="new")), self.family)
        (self.root / "toolchain/Makefile").write_text("# compiler changed\n")
        self.assertNotEqual(checkpoint.identity(self.root, "example", self.environment), self.family)

    def test_existing_build_directories_are_not_overwritten(self):
        self.prepare_state()
        with self.assertRaisesRegex(ValueError, "existing build state"):
            checkpoint.restore(self.root, self.archive, self.family)

    def test_newly_tracked_sources_cannot_be_overwritten_by_old_generated_files(self):
        self.prepare_state()
        self.discard_build_state()
        source = self.root / "scripts/config/conf"
        source.write_bytes(b"new tracked source")
        subprocess.run(["git", "add", "-f", "scripts/config/conf"], cwd=self.root, check=True)
        checkpoint.restore(self.root, self.archive, self.family)
        self.assertEqual(source.read_bytes(), b"new tracked source")

    def test_capture_preserves_custom_package_sources_for_the_next_run(self):
        package = self.root / "package/ci-custom/example"
        package.mkdir(parents=True)
        (package / "Makefile").write_text("# custom package\n")
        checkpoint.capture(self.root)
        self.assertEqual((self.root / ".ci-incremental/custom-sources/example/Makefile").read_text(), "# custom package\n")


class ArtifactDiscoveryTests(unittest.TestCase):
    def test_only_matching_nonexpired_well_formed_state_is_selected(self):
        prefix = "build-state-" + "a" * 32 + "-"
        items = [
            {"name": prefix + "4-1", "expired": True, "workflow_run": {"id": 4}},
            {"name": prefix + "3-1\ninjected=value", "expired": False, "workflow_run": {"id": 3}},
            {"name": "different-state-2-1", "expired": False, "workflow_run": {"id": 2}},
            {"name": prefix + "1-1", "expired": False, "workflow_run": {"id": 1}},
        ]
        with patch.object(checkpoint, "api_json", return_value={"artifacts": items}):
            self.assertEqual(checkpoint.discover(prefix, "owner/repo"), {"name": prefix + "1-1", "run_id": "1"})

    def test_no_matching_checkpoint_means_a_first_build(self):
        with patch.object(checkpoint, "api_json", return_value={"artifacts": []}):
            self.assertEqual(checkpoint.discover("prefix", "owner/repo"), {"name": "", "run_id": ""})


if __name__ == "__main__":
    unittest.main()
