# Crux PE13 R6 2026101104 — Actions-built, Actions-signed candidate

This build comes from the same frozen 2026101104 source and the same reviewed shard
artifacts as `r6-2026101104-cloud`, but every remaining step ran inside GitHub
Actions:

* the terminal shard was compiled by the pipeline controller
  (`coachpo/crux-pe13-distributed`, wave 7 run `38592750722`, artifact
  `shard-w007-s000`), which reused the already accepted artifacts of the other
  38 shards;
* the signing run downloaded that artifact by object id, required the worker receipt
  to name the same run, task, worker commit and slice manifest, re-hashed every
  signed input, and required `SYSTEM/build.prop` and the `BOOT`/`RECOVERY` kernels to
  match the frozen release identity and the actual cloud `Image`;
* the release keys only exist as secrets of the protected `release` environment and
  were removed from the runner when the job finished;
* the Recovery verifier was rebuilt from its retained sources and retained static
  libraries inside the same job, and its unit tests ran before the four native gates:
  APK/APEX signatures (191 APK / 27 APEX / 26 resigned), image AVB (5 native checks),
  Recovery structure inspection and the native OTA signature tool (4/4 cases);
* the delivery was published under a new immutable tag, and the feed is only moved
  after a publication run re-read the uploaded asset digest through the API.

Packaging is not byte-identical across environments, so this build is its own
artifact: it was installed with `adb reboot sideload` + `adb sideload`
(`Total xfer: 1.98x`) and verified on the device before the feed recommended it.

OTA SHA256 `e9de85769f196044d95e87a1a7b0a85199ddfbb7aa3e3579efa6b878f5fa71ed`
MD5 `dcb729463e5d23426b93db831932f328` · 1,685,382,360 bytes

Device verification: fingerprint `Xiaomi/crux/crux:13/TQ3A.230901.001.B1/2026101104:user/release-keys`,
`ro.secure=1`, `ro.debuggable=0`, `ro.adb.secure=1`, encrypted data, SELinux Enforcing,
`sys.boot_completed=1`, `ro.boot.bootreason=reboot,sideload`, paired kernel
`4.14.305-Crux-PE` (built 2026-10-12 21:58:29 UTC), Recovery dropbox carries the OTA
install record. Data witnesses are unchanged from before the install: AndroidID
`5083620eaded45a9`, witness nonce `a8d4c495-841c-4bc2-923f-bcec2bddfcfe`, Download
witness md5 `b91f3d9540b07f4a4c07986a35a5c3c1`, and the app still reports
`READY / RETAINED`.

Scope: same limits as the R6 candidate it rebuilds — no hardware, UI, CTS/VTS or TWRP
claim, and the OTA still does not write the `recovery` partition.
