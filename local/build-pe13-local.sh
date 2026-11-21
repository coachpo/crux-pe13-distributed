#!/usr/bin/env bash
# Build one local PE13 B batch (aosp_crux-userdebug) in OrbStack cruxbuild.
#
#   build-pe13-local.sh [--seed-from STATE | --resume] BUILD_NUMBER STATE_DIR EVIDENCE_DIR [MARKER [PATCH...]]
#
# Run inside cruxbuild as qingli. BUILD_NUMBER has 10 digits. STATE_DIR is a
# new managed cache, or --resume selects an existing managed cache. --seed-from
# clones only android-out with mandatory reflinks; the seed is never built into.
# Each run receives fresh source overlays. EVIDENCE_DIR is new, normally on the Mac
# through /mnt/mac, for source-state.txt, the resolved manifest and the build
# epoch. MARKER (default crux-pe13-local-BUILD_NUMBER) is baked into
# /system/etc/crux-delivery-verification.txt. Private overlays keep the shared
# PE and kernel checkouts unchanged. Each PATCH (VM path, -p1 from the PE
# source root) is applied inside the overlay and copied into EVIDENCE_DIR. The
# fingerprint tail is the build number, as in the cloud freeze. Check the tree
# first with crux-pe13-distributed/scripts/check_local_source.py: the build is
# the same code as Actions only when every project matches its pin.
# Derived from the accepted 2026110502 recipe (B/build-path-b.sh).
set -eo pipefail

src=/home/qingli/crux-pe13-offline-2026-09-25/pe13
kernel=/home/qingli/crux-kernel-cepheus
script=$(realpath -- "$0")
mode=start
if [[ ${1:-} == --mount || ${1:-} == --build ]]; then
    mode=${1#--}
    shift
    run=${1:?RUN_DIR}
    logical_out=${2:?LOGICAL_OUT}
    shift 2
fi
cache_mode=new
seed=
if [[ $mode = start && ${1:-} = --resume ]]; then
    cache_mode=resume
    shift
elif [[ $mode = start && ${1:-} = --seed-from ]]; then
    seed=${2:?SEED_STATE}
    shift 2
fi
build=${1:?BUILD_NUMBER}
state=${2:?STATE_DIR}
evidence=${3:?EVIDENCE_DIR}
marker=${4:-crux-pe13-local-$build}
patches=("${@:5}")
[[ $build =~ ^[0-9]{10}$ ]]
[[ $marker =~ ^[A-Za-z0-9._-]+$ ]]
[[ $state == /* && $evidence == /* ]]
fingerprint=Xiaomi/crux/crux:13/TQ3A.230901.001.B1/$build:userdebug/test-keys

case $mode in
    start)
        [[ $(id -un) = qingli && $(hostname -s) = cruxbuild ]]
        cd "$src"
        test "$(realpath kernel/xiaomi/crux-pe-cepheus)" = "$kernel"
        if pgrep -x 'soong_ui|ninja|ckati'; then
            echo 'An Android build process is already active; not starting another.' >&2
            exit 1
        fi
        test ! -e "$evidence"
        helper=$(dirname "$script")/local-build-state.py
        if [[ $cache_mode = resume ]]; then
            logical_out=$(python3 "$helper" logical-out "$state")
        elif [[ -n $seed ]]; then
            logical_out=$(python3 "$helper" init "$state" --seed-from "$seed")
        else
            logical_out=$(python3 "$helper" init "$state")
        fi
        mkdir -- "$evidence"
        run=$(mktemp -d "$state/run.XXXXXXXX")
        mkdir -- "$run"/{src-lower,src-upper,src-work,kernel-lower,kernel-upper,kernel-work}
        printf '%s\n' "build=$build" "marker=$marker" "fingerprint=$fingerprint" "state=$state" \
            "logical_out=$logical_out" "overlay_run=$run" \
            > "$evidence/build-identity.txt"
        {
            date -Is
            df -h "$src" "$state"
            free -h
            nproc
            for project in device/xiaomi/crux "$kernel" build/make build/soong prebuilts/build-tools vendor/aosp system/core packages/modules/adb; do
                printf '\n[%s]\n' "$project"
                git -C "$project" rev-parse HEAD
                git -C "$project" status --short --branch
                git -C "$project" diff --stat HEAD
            done
            git -C device/xiaomi/crux diff HEAD
            git -C "$kernel" diff HEAD
        } > "$evidence/source-state.txt"
        for patch in "${patches[@]}"; do
            test -f "$patch"
        done
        exec sudo -n unshare --mount --propagation private bash "$script" --mount "$run" "$logical_out" "$build" "$state" "$evidence" "$marker" "${patches[@]}"
        ;;
    mount)
        test "$(id -u)" = 0
        test "$(findmnt -n -o PROPAGATION /)" = private
        cd /
        if [[ $state/android-out != "$logical_out" ]]; then
            mount --bind "$state/android-out" "$logical_out"
        fi
        for name in src kernel; do
            target=${!name}
            lower=$run/$name-lower
            mount --bind "$target" "$lower"
            mount -o remount,bind,ro "$lower"
            mount -t overlay overlay \
                -o "lowerdir=$lower,upperdir=$run/$name-upper,workdir=$run/$name-work" "$target"
        done
        exec runuser -u qingli -- bash "$script" --build "$run" "$logical_out" "$build" "$state" "$evidence" "$marker" "${patches[@]}"
        ;;
    build)
        test "$(id -un)" = qingli
        test "$(findmnt -n -o FSTYPE -T "$src")" = overlay
        test "$(findmnt -n -o FSTYPE -T "$kernel")" = overlay
        cd "$src"
        test -d "$logical_out"
        mk=device/xiaomi/crux/aosp_crux.mk
        file=device/xiaomi/crux/crux-delivery-verification.txt
        test ! -e "$file"
        if grep -Fq crux-delivery-verification.txt "$mk"; then
            echo "$mk already names a delivery marker" >&2
            exit 1
        fi
        printf '%s\n' "$marker" > "$file"
        # Same image-baked identity hunk as the accepted path-b-marker.patch.
        python3 - "$mk" <<'PY'
import sys
path = sys.argv[1]
text = open(path).read()
anchor = "# Device identifier\n"
assert text.count(anchor) == 1, "aosp_crux.mk anchor differs"
block = ("# Image-baked identity for the local Dev direct-install candidate.\n"
         "ifeq ($(TARGET_BUILD_VARIANT),userdebug)\n"
         "PRODUCT_COPY_FILES += \\\n"
         "    device/xiaomi/crux/crux-delivery-verification.txt:$(TARGET_COPY_OUT_SYSTEM)/etc/crux-delivery-verification.txt\n"
         "endif\n\n")
open(path, "w").write(text.replace(anchor, block + anchor))
PY
        git -C device/xiaomi/crux diff --check -- aosp_crux.mk
        git -C device/xiaomi/crux diff -- aosp_crux.mk > "$evidence/marker.patch"
        if ((${#patches[@]})); then
            mkdir -- "$evidence/patches"
        fi
        for patch in "${patches[@]}"; do
            patch --batch --forward --fuzz=0 -p1 --dry-run < "$patch"
            patch --batch --forward --fuzz=0 -p1 < "$patch"
            cp -- "$patch" "$evidence/patches/"
        done
        # A2 keeps the shared Make/build-tools checkouts official. Reproduce the
        # Rosetta Make settings and hide nsjail only inside this private B view.
        python3 "$(dirname "$script")/pe13-b-host-profile.py" --tree "$src" \
            --overlay-run "$run" --report "$evidence/b-host-profile.json"
        if ! /home/qingli/bin/repo manifest -r -o "$evidence/resolved-manifest.xml"; then
            echo 'Unable to export resolved manifest; see per-project source-state.txt.' >&2
        fi
        export OUT_DIR=$logical_out
        export BUILD_NUMBER=$build BUILD_FINGERPRINT=$fingerprint BUILD_DATETIME
        BUILD_DATETIME=$(python3 "$(dirname "$script")/local-build-state.py" prepare-build "$state" "$build" "$(date +%s)" \
            --record "$evidence/identity-refresh.json")
        export USE_CCACHE=0 CCACHE_DISABLE=1
        printf '%s\n' "$BUILD_DATETIME" > "$evidence/build-datetime-epoch.txt"
        source build/envsetup.sh
        lunch aosp_crux-userdebug
        test "$TARGET_PRODUCT:$TARGET_BUILD_VARIANT" = aosp_crux:userdebug
        m bootimage recoveryimage systemimage vendorimage simg2img -j14
        test "$(cat "$OUT_DIR/soong/build_number.txt")" = "$build"
        python3 "$(dirname "$script")/local-build-state.py" complete-build "$state" "$build"
        ;;
esac
