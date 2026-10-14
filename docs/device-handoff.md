# Device handoff interface

The pipeline ends at a published, signed OTA and a feed that recommends it. A device
is never touched by a workflow. This is the interface between the pipeline and the
person who runs the physical acceptance, so the result can be recorded the same way
every time.

## Who does what

| Step | Owner | Input |
| --- | --- | --- |
| Compile, sign, gate, publish, recommend | Actions | one `workflow_dispatch` of the controller with a build number |
| Install the published OTA on the device | person | the release tag the feed recommends, the install guide of that release |
| Record device evidence | person | the four witnesses below plus the property set |
| Decide to roll back | person | the previous accepted release and its rescue package |

A green pipeline is never evidence that the build boots. It proves the artifacts are
the ones that were compiled, signed with the release keys and gated.

## Install

1. Confirm the device is on a supported starting point (the previous accepted release
   of the same data layout) and that the bootloader stays unlocked.
2. Note the current release identity and the witnesses before installing:
   * `ro.build.version.incremental` and `ro.build.fingerprint`,
   * AndroidID (`settings get secure android_id`),
   * the application witness nonce and its `READY`/`RETAINED` state,
   * the md5 of the witness file in `/sdcard/Download/`.
3. Install the OTA that the feed recommends:
   ```sh
   adb reboot sideload
   adb sideload Crux-PE13-R6-OTA-candidate-<build>.zip
   ```
   The device reboots into PE Recovery, verifies the package signature, writes the
   images and reboots. `Total xfer: ~2x` and `ro.boot.bootreason=reboot,sideload`
   afterwards are the normal signatures.
4. If the device stops at the vendor logo, do not retry blindly. Take the previous
   accepted release's fastboot rescue package and restore only the six Android
   images; `userdata`, `metadata` and `cache` are preserved by that path.

## Evidence to capture after the install

Property set (must all agree with the release identity recorded in the release
notes):

```sh
adb shell getprop ro.build.version.incremental    # frozen build number
adb shell getprop ro.build.fingerprint            # …:user/release-keys
adb shell getprop ro.secure                       # 1
adb shell getprop ro.debuggable                   # 0
adb shell getprop ro.adb.secure                   # 1
adb shell getprop ro.crypto.state                 # encrypted
adb shell getprop sys.boot_completed              # 1
adb shell getenforce                              # Enforcing
adb shell dumpsys dropbox --print SYSTEM_RECOVERY_LOG   # OTA install record
```

Witness set (all four must be unchanged from before the install):

* AndroidID identical,
* the application witness nonce identical and its state `READY`/`RETAINED`,
* the witness file md5 in `/sdcard/Download/` identical,
* user data volume still encrypted with the same keys.

Record the observation with device serial, fingerprint, boot id, kernel build and
the collection order, next to the release tag and the OTA SHA256 the feed recommends.
`user` builds have ADB disabled by default: enabling USB debugging and authorizing
the host is a manual prerequisite of this step, not a pipeline failure.

## Rollback

* **Bad OTA, device still boots**: nothing to do; the previous release stays installed.
* **OTA installs but the device does not boot**: restore the previous accepted
  release with its fastboot rescue package (six Android images, no data wipe).
* **Recovery or boot chain unusable**: use the retained U-Boot/Recovery development
  inputs exactly as the workspace device rules require; every device write needs its
  own authorization and range check.

A rollback never reuses a tag. The feed is moved back with
`scripts/recommend_release.py` against the archived release record, followed by
`scripts/validate_site.py`.
