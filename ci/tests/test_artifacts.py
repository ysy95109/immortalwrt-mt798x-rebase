import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / "artifacts.py"
SPEC = importlib.util.spec_from_file_location("ci_artifacts", MODULE_PATH)
artifacts = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(artifacts)


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.target = self.root / "target"
        self.output = self.root / "output"
        self.target.mkdir()

    def source(self, name, content=b"firmware"):
        path = self.target / name
        path.write_bytes(content)
        return path

    def collect(self, required=()):
        return artifacts.collect_artifacts(self.target, "*device-a*", self.output, required)

    def test_selects_device_images_manifests_and_target_metadata(self):
        self.source("openwrt-device-a-sysupgrade.bin", b"upgrade")
        self.source("openwrt-device-a-initramfs.itb", b"boot")
        self.source("openwrt-device-a.manifest", b"packages")
        self.source("openwrt-device-b-sysupgrade.bin")
        self.source("openwrt-device-b.manifest")
        self.source("openwrt-device-a.bin.sha256", b"upstream checksum")
        self.source("sha256sums", b"upstream checksum")
        for name in artifacts.SHARED_METADATA:
            self.source(name, b"metadata")
        unrelated = self.root / "other-target"
        unrelated.mkdir()
        (unrelated / "version.buildinfo").write_bytes(b"wrong target")
        copied = self.collect(("sysupgrade", "initramfs"))
        expected = {
            "openwrt-device-a-sysupgrade.bin",
            "openwrt-device-a-initramfs.itb",
            "openwrt-device-a.manifest",
            *artifacts.SHARED_METADATA,
        }
        self.assertEqual(set(copied), expected)
        self.assertEqual({path.name for path in self.output.iterdir()}, expected | {"SHA256SUMS"})
        self.assertEqual((self.output / "version.buildinfo").read_bytes(), b"metadata")

    def test_compressed_images_are_collected(self):
        names = (
            "device-a-sysupgrade.bin.gz", "device-a-initramfs.itb.gz",
            "device-a-sdcard.img.zst", "device-a-factory.ubi.xz",
        )
        for name in names:
            self.source(name)
        self.assertEqual(self.collect(), sorted(names))

    def test_target_wide_manifest_matches_current_image_distribution_and_target(self):
        self.target = self.root / "bin/targets/mediatek/filogic"
        self.target.mkdir(parents=True)
        self.source("immortalwrt-mediatek-filogic-device-a-sysupgrade.bin")
        self.source("immortalwrt-mediatek-filogic.manifest", b"current packages")
        self.source("openwrt-mediatek-filogic.manifest", b"old distribution")
        self.source("immortalwrt-mediatek-filogic-device-b.manifest", b"other device")
        self.source("immortalwrt-mediatek-mt7622.manifest", b"other subtarget")
        self.assertEqual(self.collect(), ["immortalwrt-mediatek-filogic-device-a-sysupgrade.bin",
                                          "immortalwrt-mediatek-filogic.manifest"])
        self.assertEqual((self.output / "immortalwrt-mediatek-filogic.manifest").read_bytes(),
                         b"current packages")

    def test_target_wide_manifest_symlink_is_rejected(self):
        self.target = self.root / "bin/targets/mediatek/filogic"
        self.target.mkdir(parents=True)
        image = self.source("immortalwrt-mediatek-filogic-device-a-sysupgrade.bin")
        (self.target / "immortalwrt-mediatek-filogic.manifest").symlink_to(image)
        with self.assertRaisesRegex(artifacts.ArtifactError, "regular file"):
            self.collect()

    def test_missing_firmware_fails_without_creating_output(self):
        with self.assertRaisesRegex(artifacts.ArtifactError, "no nonempty firmware"):
            self.collect()
        self.assertFalse(self.output.exists())

    def test_manifest_and_unrelated_image_do_not_count_as_firmware(self):
        self.source("device-a.manifest", b"packages")
        self.source("device-a-build.json", b"{}")
        self.source("device-b-sysupgrade.bin")
        with self.assertRaisesRegex(artifacts.ArtifactError, "no nonempty firmware"):
            self.collect()

    def test_empty_image_does_not_count_or_get_copied(self):
        self.source("device-a-sysupgrade.bin", b"")
        with self.assertRaisesRegex(artifacts.ArtifactError, "no nonempty firmware"):
            self.collect()
        self.source("device-a-initramfs.itb")
        self.collect()
        self.assertFalse((self.output / "device-a-sysupgrade.bin").exists())

    def test_required_image_types_must_be_present_before_copying(self):
        for present, missing in (("sysupgrade", "initramfs"), ("initramfs", "sysupgrade")):
            with self.subTest(missing=missing):
                for path in self.target.iterdir():
                    path.unlink()
                self.source(f"device-a-{present}.bin")
                with self.assertRaisesRegex(artifacts.ArtifactError, missing):
                    self.collect(("sysupgrade", "initramfs"))
                self.assertFalse(self.output.exists())

    def test_checksums_include_nested_build_metadata_and_exclude_self(self):
        self.source("device-a-sysupgrade.bin", b"image")
        (self.output / "metadata" / "feature").mkdir(parents=True)
        (self.output / "build.config").write_bytes(b"CONFIG_TEST=y\n")
        (self.output / "BUILD_INFO.txt").write_bytes(b"source revision\n")
        (self.output / "metadata" / "feature" / "release.json").write_bytes(b'{"tag":"v1"}\n')
        (self.output / "metadata" / "profiles.json").write_bytes(b'{"profile":"device-a"}\n')
        (self.output / "metadata" / "SHA256SUMS").write_bytes(b"nested file is metadata")
        (self.output / "SHA256SUMS").write_bytes(b"old invalid checksum")
        self.collect()
        checksums = (self.output / "SHA256SUMS").read_text()
        expected_names = [
            "BUILD_INFO.txt", "build.config", "device-a-sysupgrade.bin",
            "metadata/SHA256SUMS", "metadata/feature/release.json", "metadata/profiles.json",
        ]
        expected = "".join(
            f"{hashlib.sha256((self.output / name).read_bytes()).hexdigest()}  {name}\n"
            for name in expected_names
        )
        self.assertEqual(checksums, expected)
        self.collect()
        self.assertEqual((self.output / "SHA256SUMS").read_text(), checksums)

    def test_matching_source_symlink_is_rejected(self):
        outside = self.root / "outside.bin"
        outside.write_bytes(b"untrusted")
        (self.target / "device-a-sysupgrade.bin").symlink_to(outside)
        with self.assertRaisesRegex(artifacts.ArtifactError, "regular file"):
            self.collect()
        self.assertFalse(self.output.exists())

    def test_source_directory_symlink_is_rejected(self):
        self.source("device-a-sysupgrade.bin")
        alias = self.root / "alias"
        alias.symlink_to(self.target, target_is_directory=True)
        with self.assertRaisesRegex(artifacts.ArtifactError, "symlink"):
            artifacts.collect_artifacts(alias, "*device-a*", self.output)

    def test_shared_metadata_symlink_is_rejected(self):
        image = self.source("device-a-sysupgrade.bin")
        (self.target / "profiles.json").symlink_to(image)
        with self.assertRaisesRegex(artifacts.ArtifactError, "regular file"):
            self.collect()

    def test_nested_output_symlink_is_rejected(self):
        self.source("device-a-sysupgrade.bin")
        self.output.mkdir()
        (self.output / "metadata").symlink_to(self.target, target_is_directory=True)
        with self.assertRaisesRegex(artifacts.ArtifactError, "symlink"):
            self.collect()

    def test_stale_output_image_is_rejected(self):
        self.source("device-a-sysupgrade.bin")
        self.output.mkdir()
        (self.output / "device-b-sysupgrade.bin").write_bytes(b"old build")
        with self.assertRaisesRegex(artifacts.ArtifactError, "stale artifact"):
            self.collect()

    def test_glob_cannot_traverse_directories(self):
        with self.assertRaisesRegex(artifacts.ArtifactError, "filename pattern"):
            artifacts.collect_artifacts(self.target, "../*", self.output)

    def test_output_cannot_overlap_target(self):
        self.source("device-a-sysupgrade.bin")
        with self.assertRaisesRegex(artifacts.ArtifactError, "must not overlap"):
            artifacts.collect_artifacts(self.target, "*device-a*", self.target / "output")


if __name__ == "__main__":
    unittest.main()
