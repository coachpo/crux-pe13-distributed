# Dev OTA channel (single-user, host-driven)

**Host-driven Dev OTA is the only supported update channel for the maintained
Crux PE13 ROM** (`aosp_crux-userdebug`). The public `aosp_crux_release` R6
feed (`r6-2026101104-hosted`) is frozen and is neither read nor written by
Dev OTA. The official Xiaomi MIUI recovery package is a recovery input,
not a project update channel.

To update the Dev ROM, use this host tool rather than Android Settings. The
Dev product is built as PE `UNOFFICIAL`: the common `ota.mk` adds PE's
`Updates` app only for `OFFICIAL` or `CI`, and the Settings controller
requires one of those build types plus the `org.pixelexperience.ota` package.
Only `aosp_crux_release` explicitly includes that app and the R6 URL overlay.
Neither PE's upstream OTA URLs nor the R6 overlay are a Dev update source.

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

Once the workflow is on `main` in `coachpo/crux-pe13-distributed`, use
**Deliver a verified PE13 build to dev OTA** (`release-dev.yml`). It runs on
standard `ubuntu-22.04` GitHub-hosted runners; it does not build, repackage
or replace the accepted images. The historical `assemble-dev.yml` is not a
publication entry point. The standard compile and assembly workflows do
not publish. The preparation workflow can publish build-input capsules but
reserves `dev-pe13-*` for ROM delivery. Do not publish Dev ROM assets manually.

The Release's Git tag targets `main`: the standard Actions token
[cannot tag a historical target with differing workflow files](https://docs.github.com/en/rest/releases/releases#create-a-release).
The Git tag is the channel version, not the ROM source revision. Use the
assembly/compile run links, artifact identity and delivery checksums to
trace the ROM bytes, not GitHub's generated source archive.

Roll out this workflow and the removal of the old `assemble-dev.yml`
publication step together. Until those changes are on `main`, do not use the
old workflow's `publish` input.

### 1. Choose the successful assembly run and tag

Use the ID of a successful `assemble-standard.yml` run on `main`, **not**
the compile run ID or artifact ID. Match its `cloud-fresh-rom-<compile_run>`
artifact with the accepted `build-standard.yml` cold compile and use the
build number in the accepted delivery record. Both the assembly artifact and
the cold compile summary must still be available. A failed assembly can retain
an artifact for diagnostics; it is not a delivery input.

For the [accepted 2026110303 host baseline](standard-runner-rom-build-acceptance-2026-11-05.md):

| Input | Example |
| --- | --- |
| `assembly_run` | `42059779500` |
| Cold compile (derived, not a workflow input) | `42018017251`, 39/39 slices |
| Assembly artifact (derived) | `cloud-fresh-rom-42018017251`, ID `13431937735` |
| `tag` | `dev-pe13-2026110303-v1` (candidate, not yet published) |

```sh
REPO=coachpo/crux-pe13-distributed
ASSEMBLY_RUN=42059779500
TAG=dev-pe13-2026110303-v1
```

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

For the candidate above, expect tag `dev-pe13-2026110303-v1`, build
`2026110303`, a `userdebug/test-keys` fingerprint and `release_ready: false`.
The default head should match only if no newer eligible prerelease exists.
`--no-device` intentionally skips ADB: `device: null` and `current: false`
are not evidence about what the phone is running. This check validates
discovery and release metadata, not the full downloaded images, boot or
runtime. Use `plan --tag ...` for a separate read-only download and local
verification of the images.

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
checks modify the frozen R6 feed.

## Host client prerequisites

- A device already using the accepted Crux **U-Boot + TWRP development
  chain**, with the PE partition layout. Confirm the latest explicit device
  control and the actual chain before any authorized write. If the device
  is on stock-ABL R6, stop: moving to Dev is a separately authorized
  boot-chain migration, not an OTA or a Dev rollback. The Dev installer
  does not install the U-Boot chain.
- `gh` with access to the pipeline releases.
- `adb` with the Crux connected (`8bd37df4` by default) for device checks and
  installation; `check --no-device` needs neither.
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

# Optional: use already downloaded payloads (release discovery still uses GitHub)
python3 scripts/dev_ota.py plan --tag dev-pe13-2026101105-v3 \
  --delivery-dir ../../crux-pe13-devcloud-2026-10-16/cloud-delivery/dev-pe13-2026101105-v3 \
  --out ../../crux-pe13-devcloud-2026-10-16/dev-ota/v3

# Check the running device against the saved plan
python3 scripts/dev_ota.py verify \
  --out ../../crux-pe13-devcloud-2026-10-16/dev-ota/v3 --verify-seconds 300
```

`plan` never writes to the device. Only with separate device-write approval,
run `python3 scripts/dev_ota.py update --tag "$TAG" --execute` with the
verified target tag. This is destructive; it reuses the established sequence:

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

Every Dev OTA install or rollback clears `peuserdata` and `pemetadata`.
Confirm the data loss before proceeding; there is no data-preserving Dev OTA
path.

`dev-pe13-2026101105-v3` is the last device-accepted dev delivery. Keep its
release and assets. Select it explicitly with `check --no-device --tag` and
`plan --tag` before considering a rollback. Under separate device-write
authorization, the same `update --tag <older-tag> --execute` path installs
the older delivery. If the phone already reports that build number,
`--force` is required to reinstall it (also true for a different `v<n>` of
the same build). Verify the actual device, target partitions, images and
write ranges before any install. Preserve the R6 rollback payload and official
MIUI recovery package at least until a cross-version Dev update **and** rollback
pass on the device; a passing host preflight or same-version reinstall is not
that acceptance. R6 rollback is a separate product path.

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
   the runtime checks listed above. Confirm Settings offers no on-device PE ROM
   update action. A publication result is not a device acceptance result.
3. **Roll back:** with separate write approval, explicitly plan and install
   `dev-pe13-2026101105-v3`, then repeat the FIT, boot and runtime checks against
   that older delivery. Record the forward and backward build identities.
   Explicit selection does not move the channel head or restore PE user data.

## Boundaries

- Dev builds are `userdebug/test-keys`, `ro.adb.secure=0`; this channel must
  never be recommended to the public release feed.
- No `target_files`, signing, OTA ZIP, `sign.yml` or `publish.yml` is involved.
- Host-side only: no on-device updater app was added.
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
cross-version install or rollback. Build `2026110303` has separate host
compile/assembly acceptance only. Its release publication, discovery of that
new tag on GitHub, physical install, boot/runtime and version-changing
rollback have not been accepted.

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
- **Discovery:** GitHub read-only checks selected
  `dev-pe13-2026101105-v3` both by default and by explicit tag. The unpublished
  `2026110303` tag was not found. Draft and incomplete release exclusion
  were checked with local fixtures; no remote draft was created.

These are source, image and host observations, not a live Settings or
device-control check.
