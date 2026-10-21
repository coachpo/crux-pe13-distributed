# Dev OTA channel (single-user, host-driven)

This is the development-product update channel for `aosp_crux-userdebug` on the
owner's Crux. It is **not** the public release OTA feed. The public
`coachpo/crux-pe13-ota` recommendation (`r6-2026101104-hosted`) is not read or
written by this channel.

## Why GitHub Releases instead of a Pages feed

There is one user and no plan to share the development ROM. A separate public
Pages feed would add a second public surface and a second publication gate for
no benefit. The channel is therefore the set of GitHub **prereleases** named

```
dev-pe13-<build>-v<n>
```

in `coachpo/crux-pe13-distributed`. Each release carries the normal dev
delivery (`delivery-record.json`, `SHA256SUMS`, boot/recovery images, the two
FITs, sparse system/vendor). `delivery-record.json` is the channel record; the
release tag and build number are the channel ordering. The host updater
discovers the newest entry, verifies it, and drives the proven U-Boot/TWRP
install.

This is a deliberate deviation from the earlier "new Pages feed" sketch: with
one user and no sharing, the release list is the feed and there is no public
recommendation file to maintain. If a real dev feed is needed later, the
record validation in `scripts/dev_ota.py` can be reused behind a Pages
publisher.

## Prerequisites

- `gh` authenticated as an account that can read the pipeline releases.
- `adb` with the Crux connected (`8bd37df4` by default).
- `zstd`, `simg2img`; `debugfs` (host or OrbStack `cruxbuild`) for the vold
  identity check.
- The retained TWRP FIT at
  `out/crux-pe13-baseline-2026-10-05/fits/twrp-menu.itb` and the U-Boot
  console bridge `u-boot-port/scripts/uconsole_watch.py` running before
  `update --execute`.
- The deployed U-Boot environment with the cache windows:
  recovery `0x38000/0x3693`, ROM `0x3c000/0x2bae`, TWRP `0x34000/0x2924`.

## Commands

From the pipeline repository:

```sh
# Channel head and whether the device is current (read-only)
python3 scripts/dev_ota.py check
python3 scripts/dev_ota.py check --json

# Download/verify the delivery, expand system/vendor, pad the FITs, write plan.json
python3 scripts/dev_ota.py plan --tag dev-pe13-2026101105-v3 \
  --out ../../crux-pe13-devcloud-2026-10-16/dev-ota/v3

# Optional: use an already downloaded delivery directory (offline)
python3 scripts/dev_ota.py plan --tag dev-pe13-2026101105-v3 \
  --delivery-dir ../../crux-pe13-devcloud-2026-10-16/cloud-delivery/dev-pe13-2026101105-v3 \
  --out ../../crux-pe13-devcloud-2026-10-16/dev-ota/v3

# Perform the install (destructive: discards peuserdata/pemetadata)
python3 scripts/dev_ota.py update --tag dev-pe13-<build>-v<n> --execute

# Check the running device against the saved plan
python3 scripts/dev_ota.py verify --out <same-out-dir> --verify-seconds 300
```

`plan` is always read-only. `update` requires `--execute`; it reuses the
established sequence:

1. reboot to the U-Boot menu and enter the shell;
2. write the recovery/ROM FIT windows and read them back (TWRP and the MIUI /
   gap windows must keep their CRC);
3. boot TWRP and run
   `u-boot-port/scripts/install-pe13.py --discard-pe-data` with the expanded
   raw images;
4. reboot and boot the new ROM FIT;
5. verify fingerprint, v2 fstab, `/data/media/0` v2, vold identity, CE,
   `boot_completed`, no v1 policy messages and no `zygote-fatal`.

## Data policy and rollback

The v1 dev OTA discards `peuserdata`/`pemetadata` on every update. The owner
has no data to preserve on this development phone. A future data-preserving
update needs a `--keep-pe-data` path in `install-pe13.py` and its own acceptance;
it is not part of this channel yet.

The previous channel entry is the rollback:
`dev-pe13-2026101105-v3` is the current head and the installed base. Keep the
release assets; the same `plan`/`update` commands can reinstall an older tag
with `--force`. The R6 release rollback is a separate product path and is not
used by the dev OTA channel.

## Boundaries

- Dev builds are `userdebug/test-keys`, `ro.adb.secure=0`; this channel must
  never be recommended to the public release feed.
- No `target_files`, signing, OTA ZIP, `sign.yml` or `publish.yml` is involved.
- Host-side only: no on-device updater app was added.
- `check`/`plan` do not write the device; `update --execute` is the only
  destructive command and must be run with the U-Boot bridge active.
