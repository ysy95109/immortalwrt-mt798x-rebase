#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PRESET="${1:?Usage: bash ci/build.sh PRESET}"
[[ $# == 1 ]] || { echo 'Usage: bash ci/build.sh PRESET' >&2; exit 1; }

# Validate all catalog entries before any feeds, configuration or output changes.
PRESET_JSON="$(python3 ci/matrix.py --preset "$PRESET")"
jq -e '.enabled' <<<"$PRESET_JSON" >/dev/null || {
    echo "Preset $PRESET is disabled; import the verified local configuration before enabling." >&2
    exit 1
}
DEVICE="$(jq -r .device <<<"$PRESET_JSON")"
FEATURES="$(jq -r '.features | join(",")' <<<"$PRESET_JSON")"
DEVICE_JSON="$(jq -c --arg device "$DEVICE" '.[$device]' ci/devices.json)"
DEFCONFIG="$(jq -r '.config_file // empty' <<<"$PRESET_JSON")"
APPLY_COMMAND=apply-custom
if [[ -z "$DEFCONFIG" ]]; then
    DEFCONFIG="$(jq -r .defconfig <<<"$DEVICE_JSON")"
    APPLY_COMMAND=apply
fi
CONFIG_SYMBOL="$(jq -r .config_symbol <<<"$DEVICE_JSON")"
TARGET_DIR="bin/targets/$(jq -r .target_dir <<<"$DEVICE_JSON")"
IMAGE_GLOB="$(jq -r .image_glob <<<"$DEVICE_JSON")"
JOBS="${CI_JOBS:-$(nproc)}"
[[ "$JOBS" =~ ^[1-9][0-9]*$ ]] || { echo 'CI_JOBS must be a positive integer' >&2; exit 1; }

# OpenWrt includes every package and files/ regardless of the selected preset.
# Own these temporary paths exclusively, so switching presets cannot leak files.
for owned in files package/ci-custom; do
    if [[ -e "$owned" || -L "$owned" ]]; then
        echo "Use a dedicated checkout; existing path would affect this build: $owned" >&2
        exit 1
    fi
done
mkdir -p tmp output
export CI_WORK_DIR
CI_WORK_DIR="$(mktemp -d "$ROOT/tmp/ci-build.XXXXXX")"
export CI_METADATA_DIR="$CI_WORK_DIR/metadata"
export CI_REQUIRED_CONFIG="$CI_METADATA_DIR/required.config"
export CI_PACKAGE_DIR="$ROOT/package/ci-custom"
export CI_PRESET_FILE="$CI_METADATA_DIR/preset.json"
OUT="$ROOT/output/$PRESET"
rm -rf -- "$OUT"
mkdir -p "$OUT/metadata" "$CI_METADATA_DIR" files "$CI_PACKAGE_DIR"
printf '%s\n' "$PRESET_JSON" > "$CI_PRESET_FILE"
CONFIG_STARTED=0
finish() {
    local status=$?
    trap - EXIT
    if [[ "${CI_INCREMENTAL:-0}" == 1 && "$CONFIG_STARTED" == 1 ]]; then
        # Capture while feature repositories still exist. This also checkpoints
        # completed tools/packages when a later build step fails.
        python3 ci/checkpoint.py capture || status=1
    fi
    # Successful artifacts are already checksummed. Only refresh diagnostics on
    # failure, so the EXIT handler cannot change a checksummed file afterward.
    if (( status != 0 )); then
        if (( CONFIG_STARTED )); then
            cp .config "$OUT/build.config" || true
        fi
        cp -a "$CI_METADATA_DIR/." "$OUT/metadata/" || true
    fi
    rm -rf -- files "$CI_PACKAGE_DIR" "$CI_WORK_DIR"
    exit "$status"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

printf 'Preset: %s\nDevice: %s\nFeatures: %s\n' "$PRESET" "$DEVICE" "$FEATURES"
if [[ "${CI_SKIP_FEEDS_UPDATE:-0}" != 1 ]]; then
    ./scripts/feeds update -a
fi
./scripts/feeds install -a
python3 ci/config.py prepare "$DEFCONFIG" "$CONFIG_SYMBOL" "$CI_REQUIRED_CONFIG"
CONFIG_STARTED=1
IFS=',' read -r -a FEATURE_LIST <<<"$FEATURES"
for feature in "${FEATURE_LIST[@]}"; do
    [[ -n "$feature" ]] || continue
    export CI_FEATURE_OPTIONS
    CI_FEATURE_OPTIONS="$(jq -c --arg feature "$feature" '.feature_options[$feature] // {}' "$CI_PRESET_FILE")"
    bash "ci/features/$feature.sh"
done
python3 ci/config.py "$APPLY_COMMAND" .config "$CI_PRESET_FILE" "$CI_REQUIRED_CONFIG"

make defconfig
python3 ci/config.py validate .config "$CONFIG_SYMBOL" "$CI_REQUIRED_CONFIG"
if [[ "${CI_INCREMENTAL:-0}" == 1 ]]; then
    # New checkouts and re-created feature files otherwise invalidate OpenWrt's
    # timestamp-based prepared/configured/built stamps even with identical data.
    python3 ci/checkpoint.py reuse-times
fi
RESOLVED_TARGET="$(sed -n 's/^CONFIG_TARGET_BOARD="\(.*\)"$/\1/p' .config)/$(sed -n 's/^CONFIG_TARGET_SUBTARGET="\(.*\)"$/\1/p' .config)"
[[ "$TARGET_DIR" == "bin/targets/$RESOLVED_TARGET" ]] || {
    echo "Resolved target $RESOLVED_TARGET does not match $TARGET_DIR" >&2
    exit 1
}
# Stage extras only after Kconfig is valid, and before the squashfs/initramfs
# build consumes files/. An empty staging directory is created for every run.
python3 ci/files.py "$CI_PRESET_FILE" files "$CI_METADATA_DIR/rootfs-files.json"
cp .config "$OUT/build.config"
./scripts/diffconfig.sh > "$OUT/diffconfig"
cp ci/devices.json "$OUT/metadata/"
cp -a "$CI_METADATA_DIR/." "$OUT/metadata/"
{
    printf 'Build time: %s\n' "$(date -u '+%Y-%m-%d %H:%M:%S UTC')"
    printf 'Repository: %s\nSource commit: %s\n' "${GITHUB_REPOSITORY:-local}" "$(git rev-parse HEAD)"
    printf 'Preset: %s\nDevice: %s\nFeatures: %s\n' "$PRESET" "$DEVICE" "$FEATURES"
    printf '\n[feeds]\n'
    for feed in feeds/*; do
        [[ -d "$feed" && ! -L "$feed" ]] || continue
        [[ -e "$feed/.git" ]] || continue
        printf '%s: %s\n' "$feed" "$(git -C "$feed" rev-parse HEAD)"
    done
    printf '\n[custom packages]\n'
    for package in "$CI_PACKAGE_DIR"/*; do
        [[ -e "$package/.git" ]] || continue
        printf '%s: %s\n' "$package" "$(git -C "$package" rev-parse HEAD)"
        git -C "$package" diff --stat
        git -C "$package" diff > "$OUT/metadata/$(basename "$package").patch"
    done
} > "$OUT/BUILD_INFO.txt"
if [[ -f feeds.conf ]]; then
    cp feeds.conf "$OUT/metadata/feeds.conf"
else
    cp feeds.conf.default "$OUT/metadata/feeds.conf"
fi

if [[ "${CI_CONFIGURE_ONLY:-0}" == 1 ]]; then
    echo "Configuration validated; compilation skipped (CI_CONFIGURE_ONLY=1)."
    exit 0
fi

# Remove only this device's previous output before building, preventing stale
# images from making a failed or incomplete build appear successful.
if [[ -d "$TARGET_DIR" ]]; then
    find "$TARGET_DIR" -maxdepth 1 -type f -name "$IMAGE_GLOB" -delete
fi
make download -j"$JOBS"
if ! make -j"$JOBS"; then
    echo 'Parallel build failed; retrying with verbose single-thread output.' >&2
    make -j1 V=s
fi
ARTIFACT_ARGS=()
while IFS= read -r kind; do
    ARTIFACT_ARGS+=(--require "$kind")
done < <(jq -r '.required_images[]?' <<<"$DEVICE_JSON")
cp .config "$OUT/build.config"
cp -a "$CI_METADATA_DIR/." "$OUT/metadata/"
python3 ci/artifacts.py "$TARGET_DIR" "$IMAGE_GLOB" "$OUT" "${ARTIFACT_ARGS[@]}"
echo "Build complete: $OUT"
