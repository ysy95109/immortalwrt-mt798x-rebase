"""Exercise rootfs staging with local assets and mocked release downloads."""

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import stat
import struct
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

CI = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CI))
spec = importlib.util.spec_from_file_location("rootfs_files_under_test", CI / "files.py")
files = importlib.util.module_from_spec(spec)
spec.loader.exec_module(files)


class RootfsFilesTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.output = self.root / "files"
        self.metadata = self.root / "rootfs-files.json"
        (self.root / "assets").mkdir()

    def asset(self, name, content):
        path = self.root / "assets" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def assemble(self, entries=(), overlays=()):
        files.assemble({"files": list(entries), "overlays": list(overlays)},
                       self.root, self.output, self.metadata)
        return json.loads(self.metadata.read_text())

    def archive(self, members):
        path = self.root / "assets/bundle.tar.gz"
        with tarfile.open(path, "w:gz") as archive:
            for name, body, kind in members:
                info = tarfile.TarInfo(name)
                info.type = kind
                if kind == tarfile.REGTYPE:
                    info.size = len(body)
                    archive.addfile(info, io.BytesIO(body))
                else:
                    info.linkname = "../escape"
                    archive.addfile(info)
        return path

    def test_local_html_tree_and_executable_binary_are_inventoried(self):
        self.asset("doona/index.html", b"dashboard")
        self.asset("doona/assets/app.js", b"javascript")
        header = bytearray(20)
        header[:6] = b"\x7fELF\x02\x01"
        struct.pack_into("<H", header, 18, 183)
        binary = self.asset("caddy", bytes(header))
        metadata = self.assemble([
            {"id": "doona", "source": "assets/doona", "destination": "/etc/honk/doona",
             "required_files": ["index.html"]},
            {"id": "caddy", "source": "assets/caddy", "destination": "/usr/bin/caddy",
             "mode": "0755", "architecture": "aarch64", "sha256": files.sha256(binary)},
        ])
        self.assertEqual((self.output / "etc/honk/doona/index.html").read_bytes(), b"dashboard")
        self.assertEqual(stat.S_IMODE((self.output / "usr/bin/caddy").stat().st_mode), 0o755)
        inventory = {item["path"]: item for item in metadata["files"]}
        self.assertEqual(len(inventory), 3)
        self.assertEqual(inventory["/usr/bin/caddy"]["sha256"], files.sha256(binary))
        self.assertFalse((self.output / "assets").exists())

    def test_rejects_binary_architecture_mismatch(self):
        header = bytearray(20)
        header[:6] = b"\x7fELF\x02\x01"
        struct.pack_into("<H", header, 18, 62)
        self.asset("caddy", bytes(header))
        with self.assertRaisesRegex(ValueError, "architecture mismatch"):
            self.assemble([{"id": "caddy", "source": "assets/caddy", "destination": "/usr/bin/caddy",
                            "architecture": "aarch64"}])

    def test_rejects_local_sha256_mismatch(self):
        self.asset("caddy", b"binary")
        with self.assertRaisesRegex(ValueError, "SHA256 mismatch"):
            self.assemble([{"id": "caddy", "source": "assets/caddy", "destination": "/usr/bin/caddy",
                            "sha256": "0" * 64}])

    def test_overlay_order_is_explicit_and_asset_collisions_need_replace(self):
        self.asset("common/etc/example", b"common")
        self.asset("specific/etc/example", b"specific")
        self.asset("custom", b"explicit")
        entry = {"id": "custom", "source": "assets/custom", "destination": "/etc/example"}
        with self.assertRaisesRegex(ValueError, "already supplied"):
            self.assemble([entry], ["assets/common", "assets/specific"])
        self.assertEqual((self.output / "etc/example").read_bytes(), b"specific")
        import shutil
        shutil.rmtree(self.output)
        self.assemble([dict(entry, replace=True)], ["assets/common", "assets/specific"])
        self.assertEqual((self.output / "etc/example").read_bytes(), b"explicit")

    def test_staging_must_start_empty(self):
        self.output.mkdir()
        (self.output / "previous-profile-file").write_bytes(b"stale")
        with self.assertRaisesRegex(ValueError, "must be empty"):
            self.assemble()

    def test_omitting_previous_asset_does_not_leak_it_into_another_build(self):
        self.asset("caddy", b"binary")
        self.assemble([{"id": "caddy", "source": "assets/caddy", "destination": "/usr/bin/caddy"}])
        second = self.root / "second-files"
        files.assemble({"files": []}, self.root, second, self.root / "second.json")
        self.assertFalse((second / "usr/bin/caddy").exists())

    def test_missing_required_html_fails(self):
        self.asset("doona/app.js", b"js")
        with self.assertRaisesRegex(ValueError, "missing required file"):
            self.assemble([{"id": "doona", "source": "assets/doona", "destination": "/etc/honk/doona",
                            "required_files": ["index.html"]}])

    def test_tar_extracts_after_stripping_one_directory(self):
        self.archive([("release/index.html", b"dashboard", tarfile.REGTYPE)])
        self.assemble([{"id": "doona", "source": "assets/bundle.tar.gz", "archive": "tar",
                        "strip_components": 1, "destination": "/etc/honk/doona", "required_files": ["index.html"]}])
        self.assertEqual((self.output / "etc/honk/doona/index.html").read_bytes(), b"dashboard")

    def test_tar_rejects_traversal_links_and_duplicate_files(self):
        cases = [[("../escape", b"bad", tarfile.REGTYPE)],
                 [("link", b"", tarfile.SYMTYPE)],
                 [("same", b"1", tarfile.REGTYPE), ("same", b"2", tarfile.REGTYPE)]]
        for index, members in enumerate(cases):
            with self.subTest(index=index):
                archive = self.archive(members)
                with self.assertRaises(ValueError):
                    files.extract_tar(archive, self.root / f"unpacked-{index}")
        self.assertFalse((self.root / "escape").exists())

    def test_overlay_symlinks_are_rejected(self):
        self.asset("outside", b"data")
        (self.root / "assets/tree").mkdir()
        (self.root / "assets/tree/link").symlink_to(self.root / "assets/outside")
        with self.assertRaisesRegex(ValueError, "links"):
            self.assemble(overlays=["assets/tree"])

    def test_url_download_is_verified(self):
        body = b"binary download"
        with patch.object(files, "download", side_effect=lambda url, dest, **kw: dest.write_bytes(body)):
            self.assemble([{"id": "binary", "url": "https://example.com/binary", "destination": "/usr/bin/tool",
                            "sha256": hashlib.sha256(body).hexdigest(), "mode": "0755"}])
        self.assertEqual((self.output / "usr/bin/tool").read_bytes(), body)

    def test_latest_release_includes_beta_and_checks_asset_digest(self):
        archive = self.archive([("index.html", b"beta", tarfile.REGTYPE)]).read_bytes()
        asset_url = "https://github.com/test/dashboard/releases/download/v2-beta/doona-2-beta.tar.gz"
        releases = [
            {"tag_name": "v1", "draft": False, "prerelease": False, "published_at": "2026-01-01", "assets": []},
            {"tag_name": "v2-beta", "draft": False, "prerelease": True, "published_at": "2026-02-01",
             "assets": [{"name": "doona-2-beta.tar.gz", "state": "uploaded", "browser_download_url": asset_url,
                         "digest": "sha256:" + hashlib.sha256(archive).hexdigest()}]},
            {"tag_name": "draft", "draft": True, "published_at": "2026-03-01"},
        ]
        def download(url, destination, **kwargs):
            destination.write_bytes(json.dumps(releases).encode() if "api.github.com" in url else archive)
        entry = {"id": "doona", "github": {"repository": "test/dashboard", "ref": "latest", "prereleases": True,
                 "asset": "doona-{version}.tar.gz"}, "archive": "tar", "destination": "/etc/honk/doona",
                 "required_files": ["index.html"]}
        with patch.object(files, "download", side_effect=download):
            metadata = self.assemble([entry])
        self.assertEqual(metadata["sources"][0]["tag"], "v2-beta")
        self.assertEqual((self.output / "etc/honk/doona/index.html").read_bytes(), b"beta")

    def test_release_without_any_checksum_is_rejected(self):
        release = {"tag_name": "v1", "draft": False, "assets": [
            {"name": "tool", "state": "uploaded", "browser_download_url": "https://github.com/test/tool/releases/download/v1/tool"}]}
        with patch.object(files, "download", side_effect=lambda url, dest, **kw: dest.write_text(json.dumps(release))):
            with self.assertRaisesRegex(ValueError, "SHA256 digest or SHA256SUMS"):
                self.assemble([{"id": "tool", "github": {"repository": "test/tool", "ref": "v1", "asset": "tool"},
                                "destination": "/usr/bin/tool"}])


if __name__ == "__main__":
    unittest.main()
