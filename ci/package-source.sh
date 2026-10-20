#!/usr/bin/env bash
# Shared by feature modules; keep restored repositories at the same package path.
ci_checkout_package() {
    local url=$1 ref=$2 destination=$3 name=$4 cached
    cached=".ci-incremental/custom-sources/$name"
    if [[ "${CI_INCREMENTAL:-0}" == 1 && -d "$cached/.git" ]] && \
        [[ "$(git -C "$cached" remote get-url origin)" == "$url" ]]; then
        cp -a -- "$cached" "$destination"
        git -C "$destination" reset --hard HEAD
    else
        git init -q "$destination"
        git -C "$destination" remote add origin "$url"
    fi
    git -C "$destination" fetch --depth 1 origin "$ref"
    git -C "$destination" checkout --detach FETCH_HEAD
}
