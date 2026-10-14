# Crux PE13 R6 2026101104 — Actions-built, Actions-signed candidate

This build comes from the same frozen 2026101104 source and the same reviewed shard
artifacts as `r6-2026101104-cloud`, but every remaining step ran inside GitHub:

* the terminal shard was compiled by the pipeline controller
  (`coachpo/crux-pe13-distributed` controller run, wave 7 run `38592750722`,
  artifact `shard-w007-s000`), which reused the already accepted artifacts of the
  other 38 shards;
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
  after a publication run re-reads the uploaded asset digest through the API.

Because the packaging step is not byte-identical across environments, this build is
its own artifact: it was installed with `adb reboot sideload` + `adb sideload` and
verified on the device before the feed recommended it.

OTA SHA256 `{{OTA_SHA256}}`
MD5 `{{OTA_MD5}}` · {{OTA_BYTES}} bytes

Device verification: `{{DEVICE_SUMMARY}}`

Scope: same limits as the R6 candidate it rebuilds — no hardware, UI, CTS/VTS or
TWRP claim, and the OTA still does not write the `recovery` partition.
