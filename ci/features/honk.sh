#!/usr/bin/env bash
set -euo pipefail

: "${CI_WORK_DIR:?Run this feature through ci/build.sh}"
: "${CI_METADATA_DIR:?Run this feature through ci/build.sh}"
: "${CI_REQUIRED_CONFIG:?Run this feature through ci/build.sh}"
: "${CI_PACKAGE_DIR:?Run this feature through ci/build.sh}"
options="${CI_FEATURE_OPTIONS:-\{\}}"
jq -e 'type == "object" and (keys - ["ref", "dashboard"] | length == 0)
    and (.ref // "HEAD" | type == "string" and test("^[A-Za-z0-9_][A-Za-z0-9_./-]*$"))
    and (.dashboard // "default" | type == "string" and test("^[a-z][a-z0-9-]*$"))' <<<"$options" >/dev/null
ref="$(jq -r '.ref // "HEAD"' <<<"$options")"
dashboard="$(jq -r '.dashboard // empty' <<<"$options")"
source_url=https://github.com/QiuSimons/luci-app-honk.git
source_dir="$CI_PACKAGE_DIR/luci-app-honk"
[[ ! -e "$source_dir" && ! -L "$source_dir" ]]
work="$(mktemp -d "$CI_WORK_DIR/honk.XXXXXX")"
trap 'rm -rf -- "$work"' EXIT
source ci/package-source.sh
ci_checkout_package "$source_url" "$ref" "$work/source" luci-app-honk
commit="$(git -C "$work/source" rev-parse HEAD)"
[[ -f "$work/source/honk/Makefile" && -f "$work/source/luci-app-honk/Makefile" ]]

# Patch package defaults instead of replacing /etc/config/honk via files/;
# upstream conffile handling can preserve settings during sysupgrade.
python3 - "$work/source/honk/files/honk.config" "$dashboard" <<'PY'
from pathlib import Path
import shlex
import sys

path = Path(sys.argv[1])
lines = path.read_text().splitlines(keepends=True)
defaults = {"enabled": "0"}
if sys.argv[2]:
    defaults["dashboard"] = sys.argv[2]
section = False
sections = 0
seen = set()
for i, line in enumerate(lines):
    tokens = shlex.split(line, comments=True)
    if tokens and tokens[0] == "config":
        section = tokens == ["config", "honk", "config"]
        sections += int(section)
    elif section and len(tokens) >= 2 and tokens[0] == "option" and tokens[1] in defaults:
        key = tokens[1]
        if key in seen or len(tokens) != 3:
            sys.exit(f"Honk: invalid or duplicate UCI default: {key}")
        seen.add(key)
        indent = line[:len(line) - len(line.lstrip())]
        lines[i] = f"{indent}option {key} '{defaults[key]}'\n"
if sections != 1 or seen != set(defaults):
    sys.exit("Honk: upstream honk.config layout changed")
path.write_text("".join(lines))
PY
mv -- "$work/source" "$source_dir"
cat <<'CONFIG' | tee -a .config "$CI_REQUIRED_CONFIG" >/dev/null

# === Honk + integrated kernel BTF ===
CONFIG_KERNEL_XDP_SOCKETS=y
CONFIG_KERNEL_DEBUG_INFO=y
# CONFIG_KERNEL_DEBUG_INFO_REDUCED is not set
CONFIG_KERNEL_DEBUG_INFO_BTF=y
CONFIG_PACKAGE_honk=y
CONFIG_PACKAGE_luci-app-honk=y
CONFIG_HONK_USE_KERNEL_BTF=y
# CONFIG_HONK_USE_VMLINUX_BTF is not set
CONFIG
jq -n --arg source "$source_url" --arg ref "$ref" --arg commit "$commit" --arg dashboard "$dashboard" \
    '{feature: "honk", source: $source, ref: $ref, commit: $commit,
      defaults: {enabled: false, dashboard: (if $dashboard == "" then "upstream" else $dashboard end)}}' \
    > "$CI_METADATA_DIR/honk.json"
