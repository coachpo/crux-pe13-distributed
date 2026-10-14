# Crux PE13 R6 2026101104 — cloud rebuild with the ramdisk fix

This build comes from the **distributed public-runner compilation itself**, with the
boot-ramdisk defect fixed in the pipeline, rather than from a local correction of
the defective archive.

The `r6-2026101104` assets published earlier cannot boot: their boot ramdisk has no
`dev`, `proc`, `sys`, `mnt`, `metadata`, `second_stage_resources` and
`debug_ramdisk` directories, so Android's first-stage `init` fails its mount and
mknod calls, aborts ("Init encountered errors starting first stage") and the device
reset-loops at the vendor logo before any log or ADB channel exists. The cause was
the capsule exchange: `build/make/core/Makefile:906-913` creates those mount points
with `mkdir -p` inside the `ramdisk.img` recipe, but only declared outputs cross the
exchange, so every consumer rebuilt the staging tree from files alone and the empty
directories disappeared.

This build re-ran the two affected shards (`w004-s000`, `w007-s000`) through the
pipeline with an approved input correction that declares the seven mount-point
leaves on the `ramdisk.img` edge; the other shards reused their reviewed artifacts.
The delivered target_files now contains `BOOT/RAMDISK/{debug_ramdisk,dev,metadata,mnt,proc,second_stage_resources,sys}/`
and `IMAGES/boot.img` carries the same seven entries — verified directly in the
terminal artifact. A `mkdir-audit.py` guard and an exchange regression test now live
in the pipeline repository so this class of loss is caught before a build.

Signed locally with the release keys; host gates re-passed: APK/APEX signatures,
image AVB (root RSA4096, system chain, Boot/DTBO hashes, vendor hashtree,
system/Recovery RSA2048), Recovery inspection and the native OTA verifier (4/4
cases).

OTA SHA256 `bcd38e29b6962bf96ad7369be6b105d22b1e17339cd7fae4cbdb533604fb245c`
MD5 `3667dccf4f43bd65ba275370361c9114` · 1,685,438,359 bytes

Rebuild from `inputs-release-2026101104-v4` with the overlay
`w004-s000-ramdisk-fix.tar.zst` (sha256 `6bacb704…`); wave runs `38505229489`
(shard `w004-s000`) and `38508448613` (shard `w007-s000`), worker commit
`bdf7fb43…`. Incremental stays `2026101104`; the recommendation identity is this
OTA's SHA256. No hardware, UI, CTS/VTS or TWRP claim is made.
