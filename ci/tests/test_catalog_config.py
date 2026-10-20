"""Catalog and Kconfig safeguards, using only temporary repository fixtures."""

import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


CI = Path(__file__).resolve().parents[1]


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, CI / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


catalog = load_module("ci_catalog_under_test", "matrix.py")
configuration = load_module("ci_configuration_under_test", "config.py")
TARGET = "CONFIG_TARGET_DEVICE_mediatek_filogic_DEVICE_ruijie_rg-x60-pro"
OTHER_TARGET = "CONFIG_TARGET_DEVICE_mediatek_filogic_DEVICE_other-router"


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.parent = Path(self.temporary.name)
        self.root = self.parent / "repository"
        (self.root / "ci/features").mkdir(parents=True)
        (self.root / "ci/presets").mkdir()
        (self.root / "defconfig").mkdir()
        (self.root / "defconfig/router.config").write_text(f"{TARGET}=y\n")
        (self.root / "ci/features/wireguard.sh").write_text("#!/bin/bash\n")
        self.builds = [
            {"name": "personal", "device": "router", "features": ["wireguard"]},
            {"name": "minimal", "device": "router", "features": []},
        ]
        self.devices = {
            "router": {
                "defconfig": "defconfig/router.config",
                "config_symbol": TARGET,
                "target_dir": "mediatek/filogic",
                "image_glob": "*ruijie_rg-x60-pro*",
                "architecture": "aarch64",
            }
        }
        self.write_catalog()

    def write_catalog(self):
        for filename, build in zip(("personal", "minimal"), self.builds):
            (self.root / f"ci/presets/{filename}.json").write_text(json.dumps(build))
        (self.root / "ci/devices.json").write_text(json.dumps(self.devices))

    def test_selects_requested_preset_and_all_in_catalog_order(self):
        self.assertEqual(catalog.matrix("personal", self.root),
                         {"include": [{"name": "personal", "device": "router"}]})
        self.assertEqual([item["name"] for item in catalog.matrix("all", self.root)["include"]],
                         ["minimal", "personal"])

    def test_empty_features_are_valid(self):
        self.assertEqual(catalog.get_preset("minimal", self.root)["features"], [])

    def test_unknown_preset_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unknown preset"):
            catalog.matrix("missing", self.root)

    def test_duplicate_preset_names_are_rejected(self):
        self.builds[1]["name"] = self.builds[0]["name"]
        self.write_catalog()
        with self.assertRaisesRegex(ValueError, "filename|duplicate preset"):
            catalog.matrix("all", self.root)

    def test_unsafe_or_reserved_preset_names_are_rejected(self):
        for name in ("../escape", "x/y", "name\nnext", "$(id)", "", "all", "x" * 64):
            with self.subTest(name=name):
                self.builds[0]["name"] = name
                self.write_catalog()
                with self.assertRaisesRegex(ValueError, "Invalid or duplicate preset"):
                    catalog.matrix("all", self.root)

    def test_unknown_device_is_rejected(self):
        self.builds[0]["device"] = "unknown-router"
        self.write_catalog()
        with self.assertRaisesRegex(ValueError, "Unknown device"):
            catalog.matrix("personal", self.root)

    def test_missing_feature_script_is_rejected(self):
        self.builds[0]["features"] = ["missing-feature"]
        self.write_catalog()
        with self.assertRaisesRegex(ValueError, "feature not found"):
            catalog.matrix("personal", self.root)

    def test_duplicate_or_unsafe_features_are_rejected(self):
        for features in (["wireguard", "wireguard"], ["../wireguard"], ["wireguard", ""], "wireguard"):
            with self.subTest(features=features):
                self.builds[0]["features"] = features
                self.write_catalog()
                with self.assertRaises(ValueError):
                    catalog.matrix("personal", self.root)

    def test_invalid_unselected_preset_is_also_rejected(self):
        self.builds[1]["device"] = "missing-router"
        self.write_catalog()
        with self.assertRaisesRegex(ValueError, "Unknown device"):
            catalog.matrix("personal", self.root)

    def test_missing_defconfig_is_rejected(self):
        (self.root / "defconfig/router.config").unlink()
        with self.assertRaisesRegex(ValueError, "defconfig not found"):
            catalog.matrix("personal", self.root)

    def test_defconfig_cannot_escape_repository(self):
        outside = self.parent / "outside.config"
        outside.write_text(f"{TARGET}=y\n")
        for path in ("../outside.config", str(outside)):
            with self.subTest(path=path):
                self.devices["router"]["defconfig"] = path
                self.write_catalog()
                with self.assertRaisesRegex(ValueError, "must stay inside"):
                    catalog.matrix("personal", self.root)

    def test_defconfig_symlink_cannot_escape_repository(self):
        outside = self.parent / "outside.config"
        outside.write_text(f"{TARGET}=y\n")
        (self.root / "defconfig/link.config").symlink_to(outside)
        self.devices["router"]["defconfig"] = "defconfig/link.config"
        self.write_catalog()
        with self.assertRaisesRegex(ValueError, "must stay inside"):
            catalog.matrix("personal", self.root)

    def test_invalid_device_paths_and_symbols_are_rejected(self):
        cases = (
            ("target_dir", "../filogic"),
            ("target_dir", "mediatek/filogic/extra"),
            ("image_glob", "../*.bin"),
            ("image_glob", "router name*"),
            ("config_symbol", "CONFIG_PACKAGE_wireguard-tools"),
        )
        original = copy.deepcopy(self.devices["router"])
        for field, value in cases:
            with self.subTest(field=field, value=value):
                self.devices["router"] = dict(original, **{field: value})
                self.write_catalog()
                with self.assertRaises(ValueError):
                    catalog.matrix("personal", self.root)

    def test_each_preset_is_independent(self):
        before = catalog.get_preset("minimal", self.root)
        self.builds[0]["packages"] = {"include": ["curl"]}
        self.builds[0]["kconfig"] = {"CONFIG_KERNEL_DEBUG_INFO": True}
        self.write_catalog()
        self.assertEqual(catalog.get_preset("minimal", self.root), before)
        self.assertEqual(catalog.get_preset("personal", self.root)["packages"]["include"], ["curl"])

    def test_conflicting_packages_and_target_overrides_are_rejected(self):
        for change in ({"packages": {"include": ["curl"], "exclude": ["curl"]}},
                       {"kconfig": {"CONFIG_TARGET_BOARD": "other"}},
                       {"kconfig": {"CONFIG_PACKAGE_curl": True}},
                       {"kconfig": {"CONFIG_KERNEL_CMDLINE": "value\ninjected=y"}}):
            with self.subTest(change=change):
                self.builds[0].update(change)
                self.write_catalog()
                with self.assertRaises(ValueError):
                    catalog.load_catalog(self.root)
                for field in change:
                    del self.builds[0][field]

    def test_extra_file_validation(self):
        base = {"id": "caddy", "url": "https://example.com/caddy", "sha256": "a" * 64,
                "destination": "/usr/bin/caddy", "mode": "0755", "architecture": "aarch64"}
        for field, value in (("destination", "/../../escape"), ("mode", "755"),
                             ("architecture", "x86_64"), ("sha256", "bad"),
                             ("url", "http://example.com/caddy")):
            with self.subTest(field=field):
                self.builds[0]["files"] = [dict(base, **{field: value})]
                self.write_catalog()
                with self.assertRaises(ValueError):
                    catalog.load_catalog(self.root)
        self.builds[0]["files"] = [base]
        self.write_catalog()
        self.assertEqual(catalog.get_preset("personal", self.root)["files"], [base])

    def test_disabled_presets_are_visible_but_not_built(self):
        self.builds[0]["enabled"] = False
        self.write_catalog()
        self.assertFalse(catalog.get_preset("personal", self.root)["enabled"])
        self.assertEqual(catalog.matrix("all", self.root)["include"], [{"name": "minimal", "device": "router"}])
        with self.assertRaisesRegex(ValueError, "disabled"):
            catalog.matrix("personal", self.root)

    def test_empty_catalog_is_valid_but_cannot_start_builds(self):
        for path in (self.root / "ci/presets").glob("*.json"):
            path.unlink()
        self.assertEqual(catalog.load_catalog(self.root), [])
        with self.assertRaisesRegex(ValueError, "No enabled presets"):
            catalog.matrix("all", self.root)

    def test_verified_local_config_can_replace_family_defconfig(self):
        (self.root / "verified.config").write_text(f"{TARGET}=y\nCONFIG_PACKAGE_curl=y\n")
        self.builds[0]["config_file"] = "verified.config"
        self.write_catalog()
        self.assertEqual(catalog.get_preset("personal", self.root)["config_file"], "verified.config")
        (self.root / "verified.config").unlink()
        with self.assertRaisesRegex(ValueError, "config_file not found"):
            catalog.load_catalog(self.root)


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name) / "family.config"
        self.base.write_text("\n".join((
            "CONFIG_TARGET_mediatek=y",
            "CONFIG_TARGET_mediatek_filogic=y",
            "CONFIG_TARGET_ALL_PROFILES=y",
            f"{TARGET}=y",
            f"{OTHER_TARGET}=y",
            'CONFIG_TARGET_DEVICE_PACKAGES_mediatek_filogic_DEVICE_other-router="other-only"',
            "CONFIG_PACKAGE_kmod-mt_wifi=y",
            "CONFIG_PACKAGE_kmod-mediatek_hnat=y",
            "CONFIG_PACKAGE_kmod-warp=y",
            "CONFIG_MTK_MT_WIFI=m",
            "CONFIG_MTK_MT_WIFI_DRIVER_VERSION_7673=y",
            "CONFIG_MTK_WHNAT_SUPPORT=m",
            "CONFIG_MTK_WARP_V2=y",
            'CONFIG_MTK_WIFI_SKU_TYPE="AX6000"',
            "",
        )))

    def prepare(self):
        return configuration.prepare(self.base.read_text(), TARGET)

    def test_prepare_selects_one_device_and_preserves_wifi_hnat(self):
        prepared, required = self.prepare()
        actual = configuration.options(prepared)
        original = configuration.options(self.base.read_text())
        selected = [key for key, value in actual.items()
                    if key.startswith("CONFIG_TARGET_DEVICE_") and value == "y"]
        self.assertEqual(selected, [TARGET])
        self.assertEqual(actual[OTHER_TARGET], "n")
        self.assertEqual(actual["CONFIG_TARGET_ALL_PROFILES"], "n")
        for key, value in original.items():
            if key.startswith(("CONFIG_MTK_", "CONFIG_PACKAGE_kmod-")):
                self.assertEqual(actual[key], value, key)
        self.assertFalse(any(key.startswith("CONFIG_TARGET_DEVICE_PACKAGES_") for key in actual))
        configuration.validate(prepared, TARGET, required)

    def test_validate_rejects_an_extra_selected_device(self):
        prepared, required = self.prepare()
        with self.assertRaisesRegex(ValueError, "Expected only"):
            configuration.validate(prepared + f"\n{OTHER_TARGET}=y\n", TARGET, required)

    def test_validate_rejects_missing_selected_device(self):
        prepared, required = self.prepare()
        with self.assertRaisesRegex(ValueError, "Expected only"):
            configuration.validate(prepared.replace(f"{TARGET}=y", f"# {TARGET} is not set"),
                                   TARGET, required)

    def test_validate_rejects_required_package_dropped_or_downgraded(self):
        prepared, required = self.prepare()
        package = "CONFIG_PACKAGE_luci-app-honk"
        required += f"{package}=y\n"
        for resolved in ("", f"# {package} is not set\n", f"{package}=m\n"):
            with self.subTest(resolved=resolved):
                with self.assertRaisesRegex(ValueError, f"{package}: expected y"):
                    configuration.validate(prepared + resolved, TARGET, required)
        configuration.validate(prepared + f"{package}=y\n", TARGET, required)

    def test_validate_rejects_required_kernel_feature_dropped(self):
        prepared, required = self.prepare()
        feature = "CONFIG_KERNEL_DEBUG_INFO_BTF"
        with self.assertRaisesRegex(ValueError, f"{feature}: expected y"):
            configuration.validate(prepared, TARGET, required + f"{feature}=y\n")

    def test_validate_enforces_disabled_requirements(self):
        prepared, required = self.prepare()
        feature = "CONFIG_KERNEL_DEBUG_INFO_REDUCED"
        required += f"# {feature} is not set\n"
        configuration.validate(prepared, TARGET, required)
        with self.assertRaisesRegex(ValueError, f"{feature}: expected n"):
            configuration.validate(prepared + f"{feature}=y\n", TARGET, required)

    def test_ccache_uses_openwrt_default_directory(self):
        prepared, _ = self.prepare()
        self.assertEqual(configuration.options(prepared)["CONFIG_CCACHE_DIR"], '""')

    def test_preset_package_overrides_replace_base_choices(self):
        prepared, required = self.prepare()
        prepared += 'CONFIG_PACKAGE_curl=m\nCONFIG_PACKAGE_nano=y\n'
        configured, requirements = configuration.apply_preset(prepared, required, {
            "packages": {"include": ["curl"], "exclude": ["nano"]},
            "kconfig": {"CONFIG_KERNEL_PRINTK": True, "CONFIG_VERSION_NICK": 'a "quoted" name',
                        "CONFIG_KERNEL_LOG_BUF_SHIFT": 18},
        })
        resolved = configuration.options(configured)
        self.assertEqual(resolved["CONFIG_PACKAGE_curl"], "y")
        self.assertEqual(resolved["CONFIG_PACKAGE_nano"], "n")
        self.assertEqual(resolved["CONFIG_KERNEL_LOG_BUF_SHIFT"], "18")
        self.assertEqual(resolved["CONFIG_VERSION_NICK"], '"a \\"quoted\\" name"')
        self.assertEqual(configured.count('CONFIG_PACKAGE_curl='), 1)
        configuration.validate(configured, TARGET, requirements)

    def test_preset_cannot_disable_a_feature_requirement(self):
        prepared, required = self.prepare()
        required += 'CONFIG_PACKAGE_honk=y\n'
        with self.assertRaisesRegex(ValueError, "conflicts"):
            configuration.apply_preset(prepared, required, {"packages": {"exclude": ["honk"]}})

    def test_imported_packages_are_checked_after_deliberate_overrides(self):
        prepared, required = self.prepare()
        prepared += 'CONFIG_PACKAGE_local-package=y\nCONFIG_PACKAGE_nano=y\n'
        configured, requirements = configuration.apply_preset(prepared, required,
            {"packages": {"exclude": ["nano"]}}, strict_packages=True)
        configuration.validate(configured, TARGET, requirements)
        self.assertEqual(configuration.options(configured)["CONFIG_PACKAGE_nano"], "n")
        with self.assertRaisesRegex(ValueError, 'local-package: expected y'):
            configuration.validate(configured.replace('CONFIG_PACKAGE_local-package=y', ''), TARGET, requirements)


if __name__ == "__main__":
    unittest.main()
