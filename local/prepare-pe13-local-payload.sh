#!/usr/bin/env bash
# Collect, verify and package one completed local PE13 B build on the Mac.
#
#   prepare-pe13-local-payload.sh [--direct-install-plan] BUILD_NUMBER STATE_DIR EVIDENCE_DIR [MARKER]
#
# STATE_DIR is the VM directory given to build-pe13-local.sh; EVIDENCE_DIR is
# the same evidence directory as a Mac path and must hold the captured
# build.log. Produces EVIDENCE_DIR/payload (four images, raw system/vendor,
# FIT inputs, kernel symbols and dual identity-free FITs), image-validation.json,
# the protected installer dry run and the unsigned
# ZIP payload (zip-payload/). The FITs, installer and ZIP payload come from the
# same code as the Actions assembly in crux-pe13-distributed. No device access.
# Sign and finalize the ZIP afterwards with crux_zip.py sign and finalize.
set -euo pipefail

scripts=$(cd -- "$(dirname -- "$0")" && pwd -P)
workspace=$(cd -- "$scripts/../.." && pwd -P)
direct_plan=false
if [[ ${1:-} = --direct-install-plan ]]; then
    direct_plan=true
    shift
fi
build=${1:?BUILD_NUMBER}
state=${2:?STATE_DIR}
evidence=$(cd -- "${3:?EVIDENCE_DIR}" && pwd -P)
marker=${4:-crux-pe13-local-$build}
manifest=$workspace/out/crux-pe13-device-validation-2026-11-05/RAM-TWRP/v2/candidate.json
manifest_sha256=3ca1a48923cf4a432fbe7a70a6f0066cd7cf650a5f9ff9daf34375f650dadc7d
[[ $build =~ ^[0-9]{10}$ ]]

grep -Fq '#### build completed successfully' "$evidence/build.log"
orb -m cruxbuild python3 "/mnt/mac$scripts/local-build-state.py" run "$state" -- \
    bash -s -- "/mnt/mac$evidence" "/mnt/mac$workspace" "$state" "$build" "$marker" <<'VM'
set -euo pipefail
evidence=$1
workspace=$2
state=$3
build=$4
marker=$5
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
    --dualboot --development-adb --without-identity \
    --source-contract "$contracts/source-declarative-boot-contract.json" --output-dir "$payload/fits" \
    --mkimage "$workspace/crux-uboot/.output-tools/tools/mkimage"
(
    cd -- "$payload/fits"
    shasum -a 256 -c FIT-SHA256SUMS
)
python3 "$scripts/install-pe13.py" \
    --system "$payload/system.raw.img" \
    --system-sha256 "$(awk '$2 == "./system.raw.img" { print $1 }' "$evidence/vm-payload-SHA256SUMS")" \
    --vendor "$payload/vendor.raw.img" \
    --vendor-sha256 "$(awk '$2 == "./vendor.raw.img" { print $1 }' "$evidence/vm-payload-SHA256SUMS")" \
    --out "$evidence/install-dry-run" --dry-run | tee "$evidence/install-dry-run.json"
if $direct_plan; then
    python3 "$scripts/direct-install-pe13.py" plan \
    --system "$payload/system.raw.img" --vendor "$payload/vendor.raw.img" \
    --recovery-fit "$payload/fits/recovery-cepheus.itb" --rom-fit "$payload/fits/rom-cepheus.itb" \
    --identity "$evidence/image-validation.json" \
    --ram-twrp-manifest "$manifest" --ram-twrp-manifest-sha256 "$manifest_sha256" \
    --out "$evidence/direct-install-plan" | tee "$evidence/direct-install-plan.json"
fi
make -s -C "$actions/installer"
python3 "$actions/scripts/crux_zip.py" prepare \
    --system "$payload/system.raw.img" --vendor "$payload/vendor.raw.img" \
    --recovery-fit "$payload/fits/recovery-cepheus.itb" --rom-fit "$payload/fits/rom-cepheus.itb" \
    --installer "$actions/installer/build/crux-installer" --build-number "$build" --any-serial \
    --twrp-profile "$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["profile_id"])' \
        "$actions/config/install-twrp.json")" \
    --out "$evidence/zip-payload" | tee "$evidence/zip-payload.json"
