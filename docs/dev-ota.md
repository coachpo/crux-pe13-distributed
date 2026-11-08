# Dev OTA channel (single-user, host-driven)

**Host-driven Dev OTA is the only supported update channel for the maintained
Crux PE13 ROM** (`aosp_crux-userdebug`). The stock-ABL `aosp_crux_release`
R6 Releases and feed were deleted on 2026-11-07; Dev OTA never read or wrote
them. The official Xiaomi MIUI recovery package is a recovery input, not a
project update channel.

To update the Dev ROM, use this host tool rather than Android Settings. The
Dev product is built as PE `UNOFFICIAL`: the common `ota.mk` adds PE's
`Updates` app only for `OFFICIAL` or `CI`, and the Settings controller
requires one of those build types plus the `org.pixelexperience.ota` package.
Only `aosp_crux_release` explicitly includes that app and the R6 URL overlay,
whose feed no longer exists. Neither PE's upstream OTA URLs nor the R6
overlay are a Dev update source.

A build that contains the Crux A update stack (updater app, prepare/result
services and the PE Recovery installer; not yet in the cloud source pins) can
instead install a **phone release** from Settings, keeping data. A phone
release is an ordinary Dev prerelease plus a signed manifest; see
[Phone release](#phone-release-signed-manifest).

## How a release becomes a dev update

There is no separate dev feed or recommendation pointer. The channel reads
published GitHub **prereleases** in `coachpo/crux-pe13-distributed` named

```
dev-pe13-<build>-v<n>
```

The updater ignores drafts, ordinary releases, tags without a release and
incomplete deliveries. An eligible prerelease must have all eight assets
uploaded:
`delivery-record.json`, `SHA256SUMS`, `boot.img`, `recovery.img`,
`rom-cepheus.itb`, `recovery-cepheus.itb`, `system.img.zst` and `vendor.img`.
Unless you pass an explicit `--tag`, it selects the greatest build number
(10 digits), then numeric `v<n>`, then publication time. The selected
delivery's record and checksums must pass validation; a bad record is an
error, not permission to fall back silently.

Publishing a complete, valid prerelease makes it discoverable as soon as
GitHub returns it in the release list. It becomes the default selection if it
sorts ahead of the current head; GitHub's “Latest” badge is irrelevant. This
does not download images automatically or initiate an install. The repository
is public, so publishing a prerelease also makes its assets public.

## Deliver an accepted standard-runner build

On `main` in `coachpo/crux-pe13-distributed`, use
**Deliver a verified PE13 build to dev OTA** (`release-dev.yml`). It runs on
standard `ubuntu-22.04` GitHub-hosted runners; it does not build, repackage
or replace the accepted images. The standard freeze, preparation, compile and
assembly workflows do not publish Releases. Do not publish Dev ROM assets
manually.

The Release's Git tag targets `main`: the standard Actions token
[cannot tag a historical target with differing workflow files](https://docs.github.com/en/rest/releases/releases#create-a-release).
The Git tag is the channel version, not the ROM source revision. Use the
assembly/compile run links, artifact identity and delivery checksums to
trace the ROM bytes, not GitHub's generated source archive.

This workflow and the removal of the old `assemble-dev.yml` publication step
landed together at `58a4325`. The October workflows themselves (`prepare.yml`,
`controller.yml`, `controller-wave.yml`, `wave.yml`, `assemble-dev.yml`) were
removed on 2026-11-07.

### 1. Choose the successful assembly run and tag

Use the ID of a successful `assemble-standard.yml` run on `main`, **not**
the compile run ID or artifact ID. Match its `cloud-fresh-rom-<compile_run>`
artifact with the accepted `build-standard.yml` cold compile and use the
build number in the accepted delivery record. Both the assembly artifact and
the cold compile summary must still be available. A failed assembly can retain
an artifact for diagnostics; it is not a delivery input.

For the accepted `2026110501` build ([README baseline](../README.md#standard-runner-build-baseline)):

| Input | Example |
| --- | --- |
| `assembly_run` | `42260118571` |
| Cold compile (derived, not a workflow input) | `42218749916`, 39/39 slices |
| Assembly artifact (derived) | `cloud-fresh-rom-42218749916`, ID `13525224011` |
| `tag` | [`dev-pe13-2026110501-v1`](https://github.com/coachpo/crux-pe13-distributed/releases/tag/dev-pe13-2026110501-v1) (published example) |

```sh
REPO=coachpo/crux-pe13-distributed
ASSEMBLY_RUN=42260118571
TAG=dev-pe13-2026110501-v1
```

A new delivery uses its own successful assembly run and a new tag. The
`2026110501` assembly artifact expires on 2026-12-06.

### 2. Preflight (read-only)

In **Actions**, open the workflow defined in `release-dev.yml`, select
**Run workflow** on `main`, enter the assembly run and tag, and choose
`operation=preflight` (the default). Or use:

```sh
gh workflow run release-dev.yml --repo "$REPO" --ref main \
  -f assembly_run="$ASSEMBLY_RUN" -f tag="$TAG" -f operation=preflight
```

Wait for the dispatched run to succeed. Use its returned URL or locate it
in Actions (`gh run list --repo "$REPO" --workflow release-dev.yml`); the
release workflow's run ID is different from `ASSEMBLY_RUN`. Inspect the
logs for the assembly run, compile run, artifact identity and verified asset
set. The workflow verifies that the cold compile and assembly succeeded and
that the delivery record and artifact bytes match their identities and
checksums. It does not create a Release, tag or asset. The
read-only workflow token may not see drafts; write operations recheck the
actual Release state. If an artifact has expired or any identity/checksum
differs, stop. A rebuild is a new delivery, not a substitute for the accepted
artifact.

### 3. Prepare a draft (separate authorization)

After approval to create a remote draft, repeat with the same run and tag:

```sh
gh workflow run release-dev.yml --repo "$REPO" --ref main \
  -f assembly_run="$ASSEMBLY_RUN" -f tag="$TAG" -f operation=draft
```

The draft remains unpublished. Inspect the run and the draft in GitHub:
all eight assets must match the verified delivery. With an account that can
see the draft, confirm the client still selects the old channel head and
cannot select the draft tag:

```sh
CACHE="$(mktemp -d)"
python3 scripts/dev_ota.py --cache "$CACHE" check --no-device --json
# Expected to fail with "channel tag not found" while this tag is a draft.
python3 scripts/dev_ota.py --cache "$CACHE" check --no-device --json --tag "$TAG"
```

Do not manually edit or publish the draft, its tag or assets while the
workflow is running. The workflow serializes its own runs; it cannot lock
other Release writers.

### 4. Explicitly publish the verified draft (separate authorization)

After approval to make the update discoverable, dispatch **another** run with
the same inputs and `operation=publish`:

```sh
gh workflow run release-dev.yml --repo "$REPO" --ref main \
  -f assembly_run="$ASSEMBLY_RUN" -f tag="$TAG" -f operation=publish
```

The workflow revalidates the source and the draft's full remote asset set
before changing the draft to a published prerelease. It does not repair a
partial draft during publication. Wait for success and verify both explicit
selection and the default head with a fresh local metadata cache:

```sh
CACHE="$(mktemp -d)"
python3 scripts/dev_ota.py --cache "$CACHE" check --no-device --json --tag "$TAG"
python3 scripts/dev_ota.py --cache "$CACHE" check --no-device --json
```

For the example above, expect tag `dev-pe13-2026110501-v1`, build
`2026110501`, a `userdebug/test-keys` fingerprint and `release_ready: false`.
The default head should match only if no newer eligible prerelease exists.
`--no-device` intentionally skips ADB: `device: null` and `current: false`
are not evidence about what the phone is running. This check validates
discovery and release metadata, not the full downloaded images, boot or
runtime. Use `plan --tag ...` for a separate read-only download and local
verification of the images.

### Phone release: signed manifest

A phone release adds `crux-ota-manifest.txt` and `crux-ota-manifest.sig` to
the eight delivery assets. The phone updater considers only published
`dev-pe13-*` prereleases with both files, a signature that verifies with its
built-in A key, and a manifest that names this phone's serial and its
installed build among `start_builds`. It then downloads `system.img.zst`,
`vendor.img` and `rom-cepheus.itb` from the same release and checks each
size and SHA-256. The host client ignores the two extra files.

`scripts/dev_ota_manifest.py` defines the manifest (`crux-ota/1`) and
derives it from the verified delivery:
- target build and fingerprint;
- delivered and expanded size and SHA-256 of system and vendor;
- the ROM FIT, which must fit the `0x3100`-block landing window;
- the assembly run, compile run and artifact digest;
- three release inputs:
  - the installed builds allowed to update;
  - the serial the FITs carry (`any` only for FITs without one);
  - the Recovery-container U-Boot role.

FITs of builds up to `2026110704` carry the captured device identity
(`androidboot.serialno=8bd37df4`, `cpuid` and the other ABL tokens), so their
phone releases are for that phone only. From 2026-11-08 the assembly packages
both FITs `--without-identity`: the identity-injecting U-Boot copies those
tokens from the ABL command line before each PE boot (`crux_ota bootargs`).
Such FITs boot only through the Recovery container and an identity-injecting
Boot U-Boot; `msm_drm.dsi_display0` also selects the panel. Their phone
release may use `any`.

The target build must itself contain the A update stack: `crux-installer`,
`crux_ota.rc`, the CruxUpdater app with its privapp allowlist, and the A key
of `keys/crux-ota-a-p256.pub`. Otherwise, after installing it, the phone could
update again only through the host. Every stage expands `system.img.zst` and
checks these files with `debugfs` (e2fsprogs; set `DEBUGFS` when it is not on
`PATH`), so a phone release of a build without the stack is refused. The
phone must also run a listed start build behind the Recovery-container
U-Boot, with the matching PE Recovery FIT in its cache window.

**1. Sign offline.** The A private key stays outside every repository,
Actions secret and synced note. With `gh` signed in:

```sh
OUT="$(mktemp -d)/phone"
# macOS with Homebrew e2fsprogs: export DEBUGFS=/opt/homebrew/opt/e2fsprogs/sbin/debugfs
python3 scripts/dev_ota_manifest.py sign --assembly-run "$ASSEMBLY_RUN" --tag "$TAG" \
  --start-builds 2026110701 --serial 8bd37df4 \
  --key ~/.crux-keys/crux-a-ota-2026-11-07.pem --out "$OUT"
```

It repeats the workflow's source verification, writes both files to `$OUT`
and prints the manifest, its SHA-256 and `ota_signature`. The signature is
not secret; it appears in the dispatch inputs and the run log.

**2. Preflight, draft and publish with the same three extra inputs.** Each
stage keeps its own authorization:

```sh
gh workflow run release-dev.yml --repo "$REPO" --ref main \
  -f assembly_run="$ASSEMBLY_RUN" -f tag="$TAG" -f operation=preflight \
  -f ota_start_builds=2026110701 -f ota_serial=8bd37df4 -f ota_signature="$SIGNATURE"
```

Every stage regenerates the manifest from the verified delivery and refuses
a signature that does not verify with `keys/crux-ota-a-p256.pub`. The
release body records the manifest SHA-256 next to the source line. Every
operation on a phone release needs all three inputs. Without them, the
workflow treats the release as host-only and rejects its phone files as
unexpected assets. A host-only draft cannot gain phone files: use a new
`v<n>`.

### Stop, retry or roll back

| Situation | Action |
| --- | --- |
| Stop before publication | Do not dispatch `publish`. A draft can stay in place; neither it nor a failed draft upload changes the head. |
| Interrupted draft upload | Rerun `draft` with the same run and tag to upload only missing assets. If GitHub left an unfinished asset in `starter` state, stop; use a fresh version after preflight or obtain separate approval to remove that draft-only asset before retrying. |
| Same tag and same complete delivery | An identical rerun does not upload or replace assets. An already published matching prerelease is left as-is. |
| Tag, release or asset conflict | Stop. Do not overwrite, delete or repurpose a published version. Verify the existing version's identity; if intentionally publishing different accepted bytes for the same build, preflight a new `v<n>`. |
| Final publish response is lost or times out | Check GitHub and run the read-only client check before retrying: the server may have completed the final state transition. Reconcile the actual state using the same run and tag. |
| Stop after publication | Pause installs. The automatic head stays on the highest eligible published version; planning an older version does not change it. Use explicit `--tag dev-pe13-2026101105-v3` for the known rollback entry. Changing remote visibility or deleting anything requires a separate decision and authorization. |

Dispatching with `draft` or `publish`, pushing the workflow to GitHub,
altering remote releases/assets, and installing on a device are separate
actions with separate authorization. Neither this workflow nor its client
checks read or write any other update channel.

## Host client prerequisites

- A boot-complete Crux Dev ROM on the accepted **U-Boot development chain**
  with the PE partition layout. Obtain the exclusive device window before
  accessing the phone, and separate permission for any reboot or persistent
  write. Confirm the USB serial and starting fingerprint. A stock-ABL
  install is not an accepted A OTA starting point. Neither is a local
  direct-install ROM whose fingerprint tail is not its 10-digit build number:
  B `2026110502` reports `1907`. The updater does not migrate either, and
  does not install U-Boot.
- `gh` with access to the pipeline releases, `zstd` and `simg2img`;
  `debugfs` (host or OrbStack `cruxbuild`) for the image's vold identity.
  `adb` and `fastboot` are needed for installation, not for `check --no-device`
  or `plan --no-device`.
- For installation, export `CRUX_WORKSPACE` as the absolute path to the
  local Crux workspace. Without it, the client assumes the maintained
  layout, in which this checkout is `$CRUX_WORKSPACE/crux-pe13-distributed`.
  Use the workspace's
  `tools/edl/.venv/bin/python` with PySerial and PyUSB/libusb; the latter
  supplies the exact U-Boot `continue` fallback if the SDK CLI lacks it.
  Use the workspace's matching `u-boot-port/scripts/ram-boot-pe13.py`,
  `install-pe13.py`, and a single reconnecting `uconsole_watch.py` bridge.
- Use the verified **RAM-only TWRP v2** from
  `out/crux-pe13-device-validation-2026-11-05/RAM-TWRP/v2/`, with its
  reviewed manifest and the actual FIT. The installer accepts FIT SHA-256
  `3dab88d57ea205ac54db167282a0547461eee81a1e6eebdfdc528ba4e348e808`
  and profile
  `d9e985bf6ad11b6143787603898f041764813026d0da6d3b001c4cef2a8d4297`.
  The original workspace `candidate.json` SHA-256 is
  `3ca1a48923cf4a432fbe7a70a6f0066cd7cf650a5f9ff9daf34375f650dadc7d`.
  The manifest uses an absolute FIT path. Review any relocated manifest
  independently and use its own digest; the accepted FIT bytes and profile
  must still match. These are existing local artifacts, not new Release
  assets.
- Preserve the original
  `out/crux-pe13-baseline-2026-10-05/fits/twrp-menu.itb` as a **CRC witness
  only** for cache LBA `0x34000` × `0x2924` (CRC `effcee45`). Do not boot
  it for this installation; its startup can write shared `misc` and mount
  original volumes. The two PE FIT write windows are recovery
  `0x38000/0x3b00` and ROM `0x3c000/0x3100` on LUN 0, 4096 B/block: the
  whole lengths U-Boot reads for each FIT, the same windows crux-install/1
  lands (since 2026-11-07). Plans prepared for the earlier
  `0x3693`/`0x2bae` windows no longer pass; prepare and review them again.

## Commands

From the pipeline repository, prepare the selected published delivery
without contacting the phone. Choose an unused plan directory and an
explicit tag:

```sh
# Set CRUX_WORKSPACE, CACHE, and PLAN_OUT to absolute local paths first.
export CRUX_WORKSPACE
TAG=dev-pe13-2026110501-v1
python3 scripts/dev_ota.py --cache "$CACHE" check --no-device --json --tag "$TAG"
python3 scripts/dev_ota.py --cache "$CACHE" plan --no-device --tag "$TAG" \
  --out "$PLAN_OUT"
```

`plan` downloads and verifies the eight assets, expands the raw images,
pads the PE FITs and writes `plan.json`. It never writes to the device.
`--delivery-dir` can reuse an already verified local delivery, but the tag
is still resolved and validated against the public channel.

Before the first install, rehearse the keyless **no-write** control loop
under an authorized device window. Start the single workspace watcher with
an empty command bridge; use ordinary `adb reboot`, not an ABL Fastboot
or Recovery target. Enter the observed U-Boot menu's shell, verify the live
environment and watchdog, and stage the reviewed v2 FIT into RAM using
`ram-boot-pe13.py --mode recovery --resume-shell --strict-console` with
the exact FIT digest, watcher PID and fresh journal. The running installer
must pass `install-pe13.py --preflight-only --ram-twrp-id <profile-id>`.
Return via ordinary reboot, fresh U-Boot and its guarded ROM entry to the
original Dev fingerprint with a new boot ID. No physical keys,
`fastboot flash` or persistent writes are part of this rehearsal.

Only with separate device-write approval, supply the exact reviewed plan
and starting fingerprint. Use a fresh `INSTALL_OUT`; do not reuse
`PLAN_OUT`.

```sh
RAM_TWRP_MANIFEST="$CRUX_WORKSPACE/out/crux-pe13-device-validation-2026-11-05/RAM-TWRP/v2/candidate.json"
RAM_TWRP_MANIFEST_SHA256=3ca1a48923cf4a432fbe7a70a6f0066cd7cf650a5f9ff9daf34375f650dadc7d
"$CRUX_WORKSPACE/tools/edl/.venv/bin/python" scripts/dev_ota.py update \
  --tag "$TAG" --execute --delivery-dir "$CACHE/$TAG" \
  --reviewed-plan "$PLAN_OUT/plan.json" \
  --expected-start-fingerprint "$EXPECTED_START_FINGERPRINT" \
  --ram-twrp-manifest "$RAM_TWRP_MANIFEST" \
  --ram-twrp-manifest-sha256 "$RAM_TWRP_MANIFEST_SHA256" --out "$INSTALL_OUT"
```

Execution refuses the old cache-TWRP route. It checks the same reviewed
payload and starting ROM, rehearses the guarded RAM recovery and return to
the starting ROM, and reads live LUN/GPT geometry. It stages and writes
only the two approved PE FIT windows; checks readback and individual
MIUI, TWRP and gap CRCs; then stages the same protected RAM TWRP to write
and read back PE system/vendor and format PE data/metadata. The ROM
handoff must show a successful 30-second watchdog start. On failure,
keep the journal and identify the endpoint; no automatic retry or
alternate install is authorized.

Every reboot to the shell checks the Boot U-Boot version that the plan
records. Since 2026-11-08 the Boot partition holds the identity build
`2026.10-rc5-00617-g0f6d3c792bd5`, the default `--uboot-version`; plans
reviewed against the earlier `2026.10-rc5-00614-g35579e6f8c14-dirty` no
longer pass. That build keeps `bootcmd`, the menu and the `boot_pe_rom` the
client uses, so the client still boots the ROM without injecting identity:
use it only with FITs that carry the device identity.

The built-in checks verify fingerprint, CE, FBE v2 fstab/media policy,
vold identity, boot completion and absence of `zygote-fatal` at the
initial check. For device acceptance also record actual PE mounts, image
marker state, one boot ID over at least 300 seconds and continuous logs
without `zygote-fatal`. The workspace's tested `verify-pe-rom.py` in
`out/crux-pe13-device-validation-2026-11-05/device/` supplies this
supplemental observation from root ROM ADB; a timed boot-completed
recheck alone does not prove stability.

## Data policy and rollback

Every Dev OTA install or rollback clears `peuserdata` and `pemetadata`.
Confirm the data loss before proceeding; data-preserving upgrade was not
tested. PE Recovery FIT write/readback does not establish independent
Recovery boot.

Keep the verified rollback release `dev-pe13-2026101105-v3` and its assets.
Select it explicitly with `check --no-device --tag` and
`plan --no-device --tag`; use that plan and the actual running newer Dev
fingerprint with the same protected update entry after write approval.
`--force` applies only to separately reviewed same-build reinstalls;
it was not used in the accepted upgrade or rollback. The physical rollback
does not move the default channel head. R6 was deleted on 2026-11-07, so the
only rescue path outside Dev OTA is official MIUI/EDL.

## Acceptance after authorization

1. **Publish:** with separate permissions for rollout, draft and publication,
   run the [three steps above](#deliver-an-accepted-standard-runner-build).
   Verify that default discovery stays on the previous tag while the candidate
   is a draft, then check the published tag explicitly and as the default head.
   Check that the older Dev tag is still selectable and its assets remain intact.
2. **Install:** confirm device control, serial, development boot chain, write
   ranges, and the accepted rollback inputs. Run a read-only plan for the exact
   published tag. Only with device-write approval run the install, then check
   FIT readback and unchanged neighboring windows, PE-only writes, ROM boot and
   the runtime checks listed above. For a build without the update stack,
   confirm Settings offers no on-device PE ROM update action. A publication
   result is not a device acceptance result.
3. **Roll back:** with separate write approval, explicitly plan and install
   `dev-pe13-2026101105-v3`, then repeat the FIT, boot and runtime checks against
   that older delivery. Record the forward and backward build identities.
   Explicit selection does not move the channel head or restore PE user data.

## Boundaries

- Dev builds are `userdebug/test-keys`, `ro.adb.secure=0`; this channel must
  never be recommended to the public release feed.
- No `target_files`, signing, OTA ZIP, `sign.yml` or `publish.yml` is involved.
- The host client is host-side. Builds with the update stack (from
  `2026110704`) also carry the on-device CruxUpdater, which installs only
  signed [phone releases](#phone-release-signed-manifest).
- `check`/`plan` do not write the device; `update --execute` is the only
  destructive command and must be run with the U-Boot bridge active.

## Verification (2026-10-21)

The channel was exercised against the installed
`dev-pe13-2026101105-v3`:

- `check` reported the channel head and `current: true`.
- `plan` reproduced raw system `f4829db1…`, raw vendor `aec345d5…`,
  recovery FIT `d3d7d6c3…`, ROM FIT `0471e38d…`, and vold `7ec9ae3c…`.
- `update --execute --force` wrote the two FIT windows (readback
  `9197f881`/`8b002433`, TWRP/MIUI/gap CRC unchanged), booted TWRP, ran
  `install-pe13.py --discard-pe-data`, rebooted and booted the ROM.
- `verify --verify-seconds 300` passed: fingerprint
  `…/2026101105:userdebug/test-keys`, v2 fstab, `/data/media/0` v2, vold
  `7ec9ae3c…`, `ce_available=true`, `dmesg` v1=0, `zygote-fatal`=0, and a
  300 s stability window.

This was a forced reinstall of the then-current channel head, not a
cross-version install or rollback. It used the former cache TWRP entry,
not the current protected installer.

## Publication and discovery verification (2026-11-05)

`dev-pe13-2026110303-v1` (Release `435566991`, deleted on 2026-11-07) was
published directly from accepted assembly `42059779500`.
[Preflight](https://github.com/coachpo/crux-pe13-distributed/actions/runs/42083722097),
[draft](https://github.com/coachpo/crux-pe13-distributed/actions/runs/42083947278),
and [publish](https://github.com/coachpo/crux-pe13-distributed/actions/runs/42084533423)
passed. All eight uploaded asset digests matched the assembly. The client
ignored the draft, then selected v1 by default after publication; v3 remains
selectable explicitly. No device was contacted in these checks. This `2026110303`
v1 was not installed; the device acceptance below used `2026110501`.

## Protected RAM TWRP v2 acceptance (2026-11-07)

The local client that supplies this install path upgraded `2026101105`
to `2026110501`, then explicitly rolled back to
[`dev-pe13-2026101105-v3`](https://github.com/coachpo/crux-pe13-distributed/releases/tag/dev-pe13-2026101105-v3)
without `--force`. The two PE FITs and raw PE images matched readback;
the protected cache CRCs were unchanged. Each running ROM passed the
PE/FBE/CE/vold and marker checks and at least 300 seconds on one boot ID.
The newer [`dev-pe13-2026110501-v1`](https://github.com/coachpo/crux-pe13-distributed/releases/tag/dev-pe13-2026110501-v1)
remained the default after physical rollback. It was built in
[compile 42218749916](https://github.com/coachpo/crux-pe13-distributed/actions/runs/42218749916),
verified by [assembly 42260118571](https://github.com/coachpo/crux-pe13-distributed/actions/runs/42260118571),
and published by [run 42336984836](https://github.com/coachpo/crux-pe13-distributed/actions/runs/42336984836).
The evidence is in the Crux workspace at
`out/crux-pe13-device-validation-2026-11-05/device/a-upgrade-rollback-2026-11-07/`.
Those are dated device results, not device access or authorization for a
new install.

## Phone release acceptance (2026-11-08)

[`dev-pe13-2026110704-v1`](https://github.com/coachpo/crux-pe13-distributed/releases/tag/dev-pe13-2026110704-v1)
(Release `438033894`) is the first phone release. It was built in
[compile 42427170882](https://github.com/coachpo/crux-pe13-distributed/actions/runs/42427170882)
(40 shards), verified by
[assembly 42444269612](https://github.com/coachpo/crux-pe13-distributed/actions/runs/42444269612),
and signed offline with start build `2026110703` and serial `8bd37df4`.
[Preflight](https://github.com/coachpo/crux-pe13-distributed/actions/runs/42447092097),
[draft](https://github.com/coachpo/crux-pe13-distributed/actions/runs/42447377892)
and [publish](https://github.com/coachpo/crux-pe13-distributed/actions/runs/42447601669)
passed with the same `ota_*` inputs. All ten uploaded asset digests matched the
verified delivery and the signed manifest. With a fresh cache, `check
--no-device` selected it both explicitly and as the default head. The
draft-stage check that the default stayed on the previous head was not run.

On `8bd37df4`, running local build `2026110703`, Settings → System →
Additional system updates opened CruxUpdater, which read this release from
its built-in GitHub channel. The phone had no network of its own, so the host
relayed only GitHub HTTPS tunnels (`api.github.com`, `github.com`,
`release-assets.githubusercontent.com`) through `adb reverse` and the global
proxy setting. The app verified the signature, downloaded and checked the
three assets, and its PE Recovery installed them with data kept. The phone
came back on `2026110704` with the marker `crux-pe13-dev-2026110704`, an
unchanged data witness and FBE v2 policy, the ROM window CRC that the host
predicted and unchanged Recovery, TWRP and `misc` regions. It then passed 300
seconds on one boot ID and the update-stack check. The same flow over the
phone's own network was not tested. The evidence is in the Crux workspace at
`out/crux-pe13-convergence-2026-11-07/device/a-2026110704/` and
`out/crux-pe13-convergence-2026-11-07/cloud-2026110704/`.

## ROM entry-point audit (2026-11-05)

- **Source:** `vendor/aosp/config/{branding,ota}.mk` defaults to
  `UNOFFICIAL` and gates the common `Updates` package. The R6 URL lives under
  `device/xiaomi/crux/overlay-release`, selected by `aosp_crux_release.mk`.
  `packages/apps/Settings` gates the regular update preference on PE build
  type and Updater presence, and separately gates the additional update
  preference on `config_additional_system_update_setting_enable`.
- **Images:** the retained v3 system image and verified
  [Actions artifact 13431937735](https://github.com/coachpo/crux-pe13-distributed/actions/runs/42059779500/artifacts/13431937735)
  (`2026110303`) both report `org.pixelexperience.build_type=UNOFFICIAL`.
  Their app layouts do not contain PE's `Updates` app. Extracted
  `SettingsGoogle.apk` contains the guarded update preferences, with the
  additional update boolean false. Google's `ConfigUpdater` is not PE's
  ROM updater. No ROM source changes or new updater were needed.
- **Discovery before publication:** GitHub read-only checks selected
  `dev-pe13-2026101105-v3` both by default and by explicit tag. The unpublished
  `2026110303` tag was not found. Draft and incomplete release exclusion
  were checked with local fixtures; no remote draft was created.

These are source, image and host observations, not a live Settings or
device-control check.
