#!/usr/bin/env bash
set -euo pipefail

: "${CI_WORK_DIR:?Run this feature through ci/build.sh}"
: "${CI_METADATA_DIR:?Run this feature through ci/build.sh}"
: "${CI_REQUIRED_CONFIG:?Run this feature through ci/build.sh}"
: "${CI_PACKAGE_DIR:?Run this feature through ci/build.sh}"
[[ -f .config && -f rules.mk ]] || { echo 'OxiDNS: run from the configured OpenWrt source root' >&2; exit 1; }

source_url=https://github.com/svenshi/luci-app-oxidns.git
source_dir="$CI_PACKAGE_DIR/luci-app-oxidns"
[[ ! -e "$source_dir" && ! -L "$source_dir" ]] || { echo "OxiDNS: source destination already exists: $source_dir" >&2; exit 1; }
mkdir -p "$CI_WORK_DIR" "$CI_METADATA_DIR" "$CI_PACKAGE_DIR"
options="${CI_FEATURE_OPTIONS:-\{\}}"
jq -e 'type == "object" and (keys - ["ref"] | length == 0)
    and (.ref // "HEAD" | type == "string" and test("^[A-Za-z0-9_][A-Za-z0-9_./-]*$"))' <<<"$options" >/dev/null
ref="$(jq -r '.ref // "HEAD"' <<<"$options")"
work=$(mktemp -d "$CI_WORK_DIR/oxidns.XXXXXX")
trap 'rm -rf -- "$work"' EXIT

git init -q "$work/source"
git -C "$work/source" remote add origin "$source_url"
git -C "$work/source" fetch --depth 1 origin "$ref"
git -C "$work/source" checkout --detach FETCH_HEAD
commit=$(git -C "$work/source" rev-parse HEAD)
[[ -f "$work/source/Makefile" ]] || { echo 'OxiDNS: upstream package Makefile is missing' >&2; exit 1; }
mv -- "$work/source" "$source_dir"

# Upstream supplies the manager; install the OxiDNS core through its LuCI page.
cat <<'CONFIG' | tee -a .config "$CI_REQUIRED_CONFIG" >/dev/null

# === OxiDNS LuCI manager ===
CONFIG_PACKAGE_luci-app-oxidns=y
CONFIG
jq -n --arg source "$source_url" --arg ref "$ref" --arg commit "$commit" \
    '{feature: "oxidns", source: $source, ref: $ref, commit: $commit, core_bundled: false}' \
    > "$CI_METADATA_DIR/oxidns.json"
printf 'OxiDNS manager source: %s\n' "$commit"
