# Distributed Crux PixelExperience 13 development build

This repository owns the GitHub Actions pipeline for the **development** Crux
PE13 product (`aosp_crux-userdebug`): the four development images and the two
U-Boot FITs, plus the single-user host-side dev OTA channel. It does not build,
sign or publish the public release product. The former release/signing/public
OTA pipeline is no longer part of this repository; its history remains in git.

The public `coachpo/crux-pe13-ota` R6 feed is frozen/legacy and is not read or
written by anything here.

## Pipeline

1. **Freeze** the development graph and pack the cold source capsules.
2. **Prepare** (`prepare.yml`) reconstructs the frozen source tree, restores the
   toolchains, re-slices the graph and re-packs every capsule. `mode=full`
   verifies the complete set against the registry; it does not need to publish
   when `output_tag` is empty.
3. **Controller** (`controller.yml`) consolidates the cold inputs, drives the
   dependency-ordered waves (`wave.yml`, `controller-wave.yml`) and records a
   producer for every shard. It has no signing or publication stage.
4. **Assemble** (`assemble-dev.yml`) packages the four images and the two FITs
   from the verified producers, runs the AVB/v1/cpio/FIT gates, and publishes
   the immutable development prerelease.
5. **Dev OTA** (`scripts/dev_ota.py`, [docs/dev-ota.md](docs/dev-ota.md))
   discovers the newest `dev-pe13-<build>-v<n>` prerelease, verifies
   `delivery-record.json`/`SHA256SUMS`, prepares the raw images and padded FITs,
   and drives the U-Boot/TWRP install. `check`, `plan` and `verify` are
   read-only; `update --execute` is the only device-writing command.

The development product is `userdebug/test-keys` with `ro.adb.secure=0`. It is
not a release payload and must never be recommended to the public release feed.

## Inputs and evidence

- `inputs/frozen-inputs-2026101105-dev.json` — frozen graph, toolchain, capsule
  and payload identities.
- `inputs/hosted-plan-2026101105-dev.json` — 39 shards across eight waves.
- `inputs/reuse-state-dev-2026101105-v3.json` — accepted producers and the
  approved `w002-s001` supplement for the terminal shard.
- `inputs/dev-producers-2026101105-v3.json` — the seven role bindings used by
  `assemble-dev.yml`.
- `scripts/graph.py`, `scripts/capsule.py`, `scripts/finalize_slices.py`,
  `scripts/prune-index.py` and `scripts/source_archives.py` implement the
  frozen graph/capsule machinery.
- `scripts/controller.py`, `scripts/worker.py`, `scripts/package_fits.py`,
  `scripts/verify_properties.py` and `scripts/assemble_dev.py` implement the
  dev build/assembly chain.
- `scripts/dev_ota.py` and `scripts/dev_ota_fits.py` implement the dev OTA
  channel.

The current accepted dev delivery is `dev-pe13-2026101105-v3` in
`coachpo/crux-pe13-distributed`; the device acceptance is recorded in the
workspace at
`out/crux-pe13-devcloud-2026-10-16/device-install-2026-10-19/ACCEPTANCE-2026-10-21-cloud-v3.md`.

## Checks

The repository uses the Python standard library `unittest` suite:

```sh
python3 -m unittest discover -s tests
```

Host prerequisites for the dev OTA are listed in
[docs/dev-ota.md](docs/dev-ota.md). The device writes require explicit
`--execute`, per-write range/image checks and the U-Boot console bridge.

## Source and dependencies

The build inputs use the frozen October 2026 Crux source selection. The
existing generated Android Ninja graph is a planning input: compilation is
performed from source in cold runner tasks. An installed ROM binary is never a
substitute for a successful distributed build.

Android source/toolchain revisions stay pinned to the PE13 build configuration.
Current Actions dependencies are `actions/checkout@v7.0.1` and
`actions/upload-artifact@v7.0.2`, both using Node.js 24.
