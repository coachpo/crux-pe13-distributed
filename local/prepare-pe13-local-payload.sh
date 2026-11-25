#!/usr/bin/env bash
# Collect, verify and package one completed local PE13 B build on the Mac.
#
#   prepare-pe13-local-payload.sh BUILD_NUMBER STATE_DIR EVIDENCE_DIR [MARKER]
#
# STATE_DIR is the VM directory given to build-pe13-local.sh; EVIDENCE_DIR is
# the same evidence directory as a Mac path and must hold the captured
# build.log. Produces EVIDENCE_DIR/payload (four images, raw system/vendor,
# FIT inputs, kernel symbols and dual identity-free FITs), image-validation.json
# and the unsigned ZIP payload (zip-payload/). The FITs, installer and ZIP payload come from the
# same code as the Actions assembly in crux-pe13-distributed. No device access.
# Sign and finalize the ZIP afterwards with crux_zip.py sign and finalize.
#
# The ZIP accepts the install TWRP profile of config/install-twrp.json. To accept a
# new install TWRP before its tool Release, set INSTALL_TWRP_PROFILES to the
# space-separated profile IDs the package accepts instead.
set -euo pipefail

scripts=$(cd -- "$(dirname -- "$0")" && pwd -P)
workspace=$(cd -- "$scripts/../.." && pwd -P)
build=${1:?BUILD_NUMBER}
state=${2:?STATE_DIR}
evidence=$(cd -- "${3:?EVIDENCE_DIR}" && pwd -P)
marker=${4:-crux-pe13-local-$build}
mkimage=${MKIMAGE:-$workspace/crux-uboot/.output-crux/xiaomi-crux/tools/mkimage}
test -x "$mkimage"
[[ $build =~ ^[0-9]{10}$ ]]

grep -Fq '#### build completed successfully' "$evidence/build.log"
orb -m cruxbuild python3 "/mnt/mac$scripts/local-build-state.py" run "$state" -- \
    bash -s -- "/mnt/mac$evidence" "/mnt/mac$workspace" "$state" "$build" "$marker" "/mnt/mac$scripts" <<'VM'
set -euo pipefail
evidence=$1
workspace=$2
state=$3
build=$4
marker=$5
scripts=$6
out=$state/android-out
if [[ -f $state/local-build-cache.json ]]; then
    out=$(python3 "$scripts/local-build-state.py" logical-out "$state")
fi
product=$out/target/product/crux
kernel=$product/obj/KERNEL_OBJ
dtbs=$kernel/arch/arm64/boot/dts/qcom
payload=$evidence/payload
test "$(cat "$out/soong/build_number.txt")" = "$build"
mkdir -- "$payload"
mkdir -- "$payload"/{fit-inputs,kernel-symbols}
cp -- "$product"/{boot.img,recovery.img} "$payload"
cp -- "$product"/{ramdisk.img,ramdisk-recovery.img} "$payload/fit-inputs"
cp -- "$kernel/arch/arm64/boot/Image" "$dtbs"/{sm8150,sm8150-v2,sm8150p,sm8150p-v2}.dtb \
    "$dtbs/crux-sm8150-overlay.dtbo" "$payload/fit-inputs"
cp -- "$kernel"/{vmlinux,System.map,.config} "$kernel/include/config/kernel.release" \
    "$kernel/include/generated/utsrelease.h" "$payload/kernel-symbols"
if grep -Fxq CONFIG_LTO_CLANG=y "$kernel/.config"; then
    cp -- "$kernel/.tmp_lto.lds" "$payload/kernel-symbols"
fi
"$out/host/linux-x86/bin/simg2img" "$product/system.img" "$payload/system.raw.img"
"$out/host/linux-x86/bin/simg2img" "$product/vendor.img" "$payload/vendor.raw.img"
export PYTHONDONTWRITEBYTECODE=1
python3 "$scripts/verify-pe13-local-images.py" "$workspace" "$product" "$payload" \
    "$build" "$marker" > "$evidence/image-validation.json"
sha256sum "$product"/{boot.img,recovery.img,system.img,vendor.img} > "$evidence/vm-native-SHA256SUMS"
(
    cd "$payload"
    find . -type f -print0 | sort -z | xargs -0 sha256sum
) > "$evidence/vm-payload-SHA256SUMS"
VM

payload=$evidence/payload
(
    cd -- "$payload"
    shasum -a 256 -c "$evidence/vm-payload-SHA256SUMS"
)
inputs=$payload/fit-inputs
actions=$workspace/crux-pe13-distributed
contracts=$actions/references/pe13-boot-contracts
python3 "$contracts/package-pe13.py" \
    --image "$inputs/Image" --base-dir "$inputs" --overlay "$inputs/crux-sm8150-overlay.dtbo" \
    --boot-ramdisk "$inputs/ramdisk.img" --recovery-ramdisk "$inputs/ramdisk-recovery.img" \
    --dualboot --development-adb \
    --source-contract "$contracts/source-declarative-boot-contract.json" --output-dir "$payload/fits" \
    --mkimage "$mkimage"
(
    cd -- "$payload/fits"
    shasum -a 256 -c FIT-SHA256SUMS
)
make -s -C "$actions/installer"
read -r -a profiles <<< "${INSTALL_TWRP_PROFILES:-$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["profile_id"])' \
    "$actions/config/install-twrp.json")}"
profile_args=()
for profile in "${profiles[@]}"; do
    [[ $profile =~ ^[0-9a-f]{64}$ ]]
    profile_args+=(--twrp-profile "$profile")
done
(( ${#profile_args[@]} ))
python3 "$actions/scripts/crux_zip.py" prepare \
    --system "$payload/system.raw.img" --vendor "$payload/vendor.raw.img" \
    --recovery-fit "$payload/fits/recovery-cepheus.itb" --rom-fit "$payload/fits/rom-cepheus.itb" \
    --installer "$actions/installer/build/crux-installer" --build-number "$build" --any-serial \
    --forbid-text "/home/$(id -un)" --forbid-text "/Users/$(id -un)" \
    "${profile_args[@]}" \
    --out "$evidence/zip-payload" | tee "$evidence/zip-payload.json"
