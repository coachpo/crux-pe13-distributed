# Local B build, install and device tools

These host tools build path B in OrbStack, package it as the same signed
crux-install/1 TWRP ZIP that the Actions chain publishes, install it, and reach
the U-Boot console. No workflow uses this directory, and LOCAL outputs are never
A build or publication inputs. Every device step needs an exclusive window from
the coordinator the owner designates and its own write authorization; every
debug kernel handoff must arm and check the U-Boot watchdog.

Paths below are relative to the workspace root
(`/Users/qingli/Documents/project/crux`). Run the Python device helpers with the
serial-capable environment: `tools/edl/.venv/bin/python crux-pe13-distributed/local/<script>.py`.

## B build

1. Check the source tree in the VM. Every project must be at the pin of
   `config/cloud-source-manifest.json` with no tracked change:

   ```sh
   orb -m cruxbuild python3 /mnt/mac/Users/qingli/Documents/project/crux/crux-pe13-distributed/scripts/check_local_source.py \
     --tree /home/qingli/crux-pe13-offline-2026-09-25/pe13
   ```

2. Build with `build-pe13-local.sh` (F2). Resume the current managed cache with
   a new 10-digit build number (B uses `NN` 01–49) and a new evidence directory:

   ```sh
   M=/mnt/mac/Users/qingli/Documents/project/crux
   orb -m cruxbuild bash $M/crux-pe13-distributed/local/build-pe13-local.sh --resume <BUILD> \
     /home/qingli/crux-pe13-a2-work-cache $M/out/<evidence> crux-pe13-local-<BUILD> > <log> 2>&1
   ```

   - `--resume` accepts only a helper-managed cache. `--seed-from STATE` starts
     a separate managed cache from an existing state with mandatory Btrfs
     reflinks; with no option the helper starts a cold cache. It never writes
     the seed.
   - Each run gets fresh private source/kernel overlays. `pe13-b-host-profile.py`
     applies the B-only Rosetta settings there (dexpreopt/profile off,
     userdebug check downgraded to a warning, nsjail hidden); the shared tree
     keeps the official contents. The profile refuses unknown fragments.
   - A private mount maps the working OUT onto the logical path
     `/home/qingli/crux-pe13-dev-2026110803/android-out`. Keep that directory
     and the earlier managed states `/home/qingli/crux-pe13-a1-work-cache` and
     `/home/qingli/crux-pe13-work-cache`; do not run another Android build
     concurrently.
   - The fingerprint tail is the build number (`BUILD_FINGERPRINT` as in the
     cloud freeze); `crux_zip.py prepare` refuses any other tail. A new identity
     invalidates only `buildinfo.prop` and `vendor/build.prop`; the success stamp
     is written only after system/vendor properties match the build number.
   - Copy the captured log to `<evidence>/build.log`.

3. Package on the Mac. `prepare-pe13-local-payload.sh <BUILD> <state> out/<evidence>`
   collects the four images, raw system/vendor, FIT inputs and kernel symbols,
   packages the identity-free FITs with `references/pe13-boot-contracts/package-pe13.py
   --without-identity`, validates the images (PE fstab routing, native guard,
   development ADB properties), runs the protected installer dry run and writes
   the unsigned payload with `scripts/crux_zip.py prepare`. `--direct-install-plan`
   also writes an F3 plan.

   `mkimage` comes from the Crux U-Boot checkout; build it once:

   ```sh
   cd crux-uboot
   export PATH="/opt/homebrew/bin:$PATH" CPATH="/opt/homebrew/include:/opt/homebrew/opt/openssl@3/include" \
     LIBRARY_PATH="/opt/homebrew/lib:/opt/homebrew/opt/openssl@3/lib"
   gmake O=.output-tools CROSS_COMPILE=aarch64-unknown-linux-gnu- xiaomi-crux_defconfig
   gmake O=.output-tools CROSS_COMPILE=aarch64-unknown-linux-gnu- tools-only
   ```

4. Sign with the B install private key (its only readable copy is
   `~/.crux-keys/crux-b-install-2026-11-07.pem`) and finalize:

   ```sh
   python3 crux-pe13-distributed/scripts/crux_zip.py sign --payload <payload> --key ~/.crux-keys/crux-b-install-2026-11-07.pem
   python3 crux-pe13-distributed/scripts/crux_zip.py finalize --payload <payload> \
     --public-key crux-pe13-distributed/keys/crux-b-install-2026-11-07.pub.pem --out <zip>
   ```

## Install entries

Both entries format `peuserdata` and `pemetadata`; there is no data-preserving
install. Neither writes Boot, dtbo, MIUI, the cache MIUI/TWRP slots or the GPT.

- **Signed ZIP.** From the Recovery-container U-Boot menu choose *Enable fastboot
  mode*, `fastboot boot` the RAM-only install TWRP v3 (`config/install-twrp.json`
  pins it), `adb push` the ZIP to `/tmp`, install it in TWRP and confirm with
  Volume Up. The installer checks the signature, serial (`any`), container U-Boot
  and partition layout, writes `pesystem`/`pevendor`, stages both FITs and
  reboots; the container lands them over the full Recovery `0x38000 × 0x3b00`
  and ROM `0x3c000 × 0x3100` windows and starts PE with the ABL identity.
- **F3 host direct install.** `direct-install-pe13.py` makes a `plan` from
  explicit images and identity (`--compare-with` checks it against a reviewed
  plan), then `install --dry-run`, `--rehearse` (starting-ROM check and no-write
  rehearsal) or `--execute` with the exact starting ROM. It stages RAM-only
  TWRP v2 with `ram-boot-pe13.py`, runs `install-pe13.py` and writes/reads back
  both FIT windows, returning through `crux_boot_pe_rom`. Redo the plan and the
  no-write rehearsal from the live endpoint before every write.
  - `install-pe13.py --dry-run` validates raw images without ADB;
    `--preflight-only --ram-twrp-id <profile>` checks the protected TWRP without
    writing; `--discard-pe-data` requires `--ram-twrp-id`.
  - `ram-boot-pe13.py` stages inline FITs, final DT/bootargs and the checked
    watchdog; completion needs a unique executed marker, not command echo.

## Keyless entry to the U-Boot console

1. Confirm the assigned serial and running fingerprint over USB ADB, check that
   no other watcher owns the serial port or `/tmp/crux-m0`, and start one watcher:

   ```sh
   mkdir -p /tmp/crux-m0 && : > /tmp/crux-m0/cmd.txt
   tools/edl/.venv/bin/python crux-pe13-distributed/local/uconsole_watch.py
   ```

2. `adb -s "$SERIAL" reboot` (an ordinary reboot). When the menu appears on USB
   CDC-ACM, write the **observed** *Drop to shell* key to `/tmp/crux-m0/cmd.txt`
   and wait for `=>`. `@menu-redraw` asks the watcher to send two raw Ctrl-N
   bytes so a late console can see the menu; do not guess keys.
3. Read `version`, `printenv bootdelay menucmd boot_go kernel_watchdog_ms
   bootmenu_0 bootmenu_1 bootmenu_2 bootmenu_3 bootmenu_7 crux_uboot_role
   crux_boot_pe_rom` and `scsi part`. Menu block counts are read capacities, not
   write ranges.
4. Return with `run crux_boot_pe_rom` (it injects the ABL identity; `run
   boot_pe_rom` keeps the FIT's bootargs). Confirm the serial, fingerprint, a
   new boot ID and `sys.boot_completed=1`, stop the watcher and report the
   endpoint to the coordinator.

If the expected menu or endpoint does not appear, stop and report it.

## Component recipes

- `build-b-twrp.py` derives the RAM-only install TWRP from RAM TWRP v2 with the
  B public key (the raw 64-byte form is in `keys/`), the in-RAM BCB sink and a new
  profile ID; `--without-identity` drops the ten ABL identity tokens. Rebuild it
  only when the B key or TWRP changes; publish the result as a new tool Release
  and update `config/install-twrp.json`.
- `package-recovery-container.py` splices a U-Boot body into the accepted
  physical Recovery v1 container (`aa5dfe75…`) and emits the changed-block patches
  and rollback files.

## Tests

`test-install-pe13.py`, `test-direct-install-pe13.py`, `test-ram-boot-pe13.py`
and `test-b-host-profile.py` run on the Mac. `test-local-build-state.py` needs the
VM's Btrfs filesystem and non-interactive mount-namespace sudo:

```sh
orb -m cruxbuild env TMPDIR=/home/qingli \
  NINJA=/home/qingli/crux-pe13-offline-2026-09-25/pe13/prebuilts/build-tools/linux-x86/bin/ninja \
  PYTHONDONTWRITEBYTECODE=1 python3 \
  /mnt/mac/Users/qingli/Documents/project/crux/crux-pe13-distributed/local/test-local-build-state.py
```
