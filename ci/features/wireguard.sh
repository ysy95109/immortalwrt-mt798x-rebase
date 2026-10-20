#!/usr/bin/env bash
set -euo pipefail

: "${CI_METADATA_DIR:?Run this feature through ci/build.sh}"
: "${CI_REQUIRED_CONFIG:?Run this feature through ci/build.sh}"
[[ -f .config && -f rules.mk ]] || { echo 'WireGuard: run from the configured OpenWrt source root' >&2; exit 1; }
options="${CI_FEATURE_OPTIONS:-\{\}}"
jq -e 'type == "object" and length == 0' <<<"$options" >/dev/null
mkdir -p "$CI_METADATA_DIR"

cat <<'CONFIG' | tee -a .config "$CI_REQUIRED_CONFIG" >/dev/null

# === WireGuard ===
CONFIG_PACKAGE_kmod-wireguard=y
CONFIG_PACKAGE_wireguard-tools=y
CONFIG_PACKAGE_luci-proto-wireguard=y
CONFIG_PACKAGE_rpcd-mod-wireguard=y
CONFIG
jq -n '{feature: "wireguard", source: "OpenWrt source tree and LuCI feed",
    packages: ["kmod-wireguard", "wireguard-tools", "luci-proto-wireguard", "rpcd-mod-wireguard"]}' \
    > "$CI_METADATA_DIR/wireguard.json"
