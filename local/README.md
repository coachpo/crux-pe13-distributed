# Local B build, packaging and device tools

These host tools build path B in OrbStack, package it as the same signed
crux-install/1 TWRP ZIP that the Actions chain publishes, and reach the U-Boot
console. No workflow uses this directory, and LOCAL outputs are never A build
or publication inputs. Every device step needs an exclusive window from the
coordinator the owner designates and its own write authorization; every debug
kernel handoff must arm and check the U-Boot watchdog.

Paths below are relative to the workspace root (`/Users/qingli/Documents/project/crux`).
Host inputs outside the repositories live in `~/Crux-Inputs/` (RAM TWRP v2 FIT,
deployed U-Boot images) and `~/Crux-Rescue/` (rescue material); the B install
private key is `~/.crux-keys/crux-b-install-2026-11-07.pem`.

Device helpers need pyserial:

```sh
python3 -m venv ~/.venvs/crux-local
~/.venvs/crux-local/bin/pip install -r crux-pe13-distributed/local/requirements.txt
```

## B build

1. Check the source tree in the VM. Every project must be at the pin of
   `config/cloud-source-manifest.json` with no tracked change:

   ```sh
   orb -m cruxbuild python3 /mnt/mac/Users/qingli/Documents/project/crux/crux-pe13-distributed/scripts/check_local_source.py \
     --tree /home/qingli/crux-pe13-offline-2026-09-25/pe13
   ```

2. Build with `build-pe13-local.sh`. Resume the one managed cache with a new
   10-digit build number (B uses `NN` 01–49) and a new evidence directory:

   ```sh
   M=/mnt/mac/Users/qingli/Documents/project/crux
   orb -m cruxbuild bash $M/crux-pe13-distributed/local/build-pe13-local.sh --resume <BUILD> \
     /home/qingli/crux-pe13-b-2026112102 $M/out/<evidence> crux-pe13-local-<BUILD> > <log> 2>&1
   ```

   - `--resume` accepts only a helper-managed cache. `--seed-from STATE` starts
     a separate managed cache from an existing one with mandatory Btrfs
     reflinks; with no option the helper starts a cold cache.
   - Each run gets fresh private source/kernel overlays. `pe13-b-host-profile.py`
     applies the B-only Rosetta settings there (dexpreopt/profile off,
     userdebug check downgraded to a warning, nsjail hidden); the shared tree
     keeps the official contents. The profile refuses unknown fragments.
   - The cache's Ninja graph names the logical OUT
     `/home/qingli/crux-pe13-dev-2026110803/android-out`; a private bind mount
     maps the cache's `android-out` onto it. Keep that (empty) directory: a
     cache with another logical path means a cold build. Do not run another
     Android build concurrently.
   - The fingerprint tail is the build number (`BUILD_FINGERPRINT` as in the
     cloud freeze); `crux_zip.py prepare` refuses any other tail. A new identity
     invalidates only `buildinfo.prop` and `vendor/build.prop`; the success stamp
     is written only after system/vendor properties match the build number.
   - Copy the captured log to `<evidence>/build.log`.
   - After sepolicy, privileged app or native module changes, first run
     `m selinux_policy sepolicy.recovery <modules>` in a throwaway overlay
     state; the full build runs about 2.5 h and neverallow checks run late.

3. Package on the Mac. `prepare-pe13-local-payload.sh <BUILD> <state> out/<evidence>`
   collects the four images, raw system/vendor, FIT inputs and kernel symbols,
   packages the identity-free FITs with `references/pe13-boot-contracts/package-pe13.py
   --without-identity`, validates the images (PE fstab routing, native guard,
   development ADB properties) and writes the unsigned payload with
   `scripts/crux_zip.py prepare`. `mkimage` is the Crux U-Boot host tool
   (`MKIMAGE` overrides it); build it once with `crux-uboot/tools/crux/build-images.sh`
   (see `crux-uboot/tools/crux/README.md`), which leaves it in
   `crux-uboot/.output-crux/xiaomi-crux/tools/mkimage`.

4. Sign with the B install private key and finalize:

   ```sh
   python3 crux-pe13-distributed/scripts/crux_zip.py sign --payload <payload> --key ~/.crux-keys/crux-b-install-2026-11-07.pem
   python3 crux-pe13-distributed/scripts/crux_zip.py finalize --payload <payload> \
     --public-key crux-pe13-distributed/keys/crux-b-install-2026-11-07.pub.pem --out <dir>
   ```

## Install

The signed ZIP is the only install entry. It formats `peuserdata` and
`pemetadata`; there is no data-preserving install. It never writes Boot, dtbo,
MIUI, the cache MIUI/TWRP slots or the GPT.

From the Recovery-container U-Boot menu choose *Enable fastboot mode*,
`fastboot boot` the RAM-only install TWRP v3 (`config/install-twrp.json` pins
it), `adb push` the ZIP to `/tmp`, install it in TWRP and confirm with Volume
Up. The installer checks the signature, serial (`any`), container U-Boot and
partition layout, writes `pesystem`/`pevendor`, stages both FITs and reboots;
the container lands them over the full Recovery `0x38000 × 0x3b00` and ROM
`0x3c000 × 0x3100` windows and starts PE with the ABL identity.

After the first boot, `verify-pe-rom.py --identity <evidence>/image-validation.json
--marker <marker> --out <new dir>` checks over root ROM ADB, read-only and for at
least 300 s, the build identity, the PE partition routing, vold/CE state and the
absence of zygote failures.

## Keyless entry to the U-Boot console

1. Confirm the assigned serial and running fingerprint over USB ADB, check that
   no other watcher owns the serial port or `/tmp/crux-m0`, and start one watcher:

   ```sh
   mkdir -p /tmp/crux-m0 && : > /tmp/crux-m0/cmd.txt
   ~/.venvs/crux-local/bin/python crux-pe13-distributed/local/uconsole_watch.py
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

`ram-boot-pe13.py --fit F --fit-sha256 H --mode recovery|rom --out D` stages a
FIT into RAM from that shell with its final DT/bootargs and the checked watchdog
for kernel debugging (`--dry-run` validates without the device); completion
needs a unique executed marker, not command echo.

## RAM-only install TWRP

`build-b-twrp.py` derives the install TWRP from the RAM TWRP v2 FIT
(`~/Crux-Inputs/twrp/ram-twrp-v2/twrp-ram-install-only.itb`, `3dab88d5…`; its DT
bootargs carry the device identity, so it stays outside the public
repositories) with the B public key (raw 64-byte form in `keys/`), the in-RAM
BCB sink and a new profile ID; `--without-identity` drops the ten ABL identity
tokens. `ram_twrp_pack.py` holds its FIT and CPIO helpers. Rerun it only when
the B key or TWRP changes and publish the result as a new tool Release, then
update `config/install-twrp.json`:

```sh
python3 crux-pe13-distributed/local/build-b-twrp.py --without-identity \
  --v2-fit ~/Crux-Inputs/twrp/ram-twrp-v2/twrp-ram-install-only.itb \
  --public-key crux-pe13-distributed/keys/crux-b-install-2026-11-07.p256.pub \
  --mkimage crux-uboot/.output-crux/xiaomi-crux/tools/mkimage --out <new dir>
```

The profile ID and the kernel, DT and ramdisk components are deterministic
(`1c11831a…` for the pinned v3); the FIT bytes carry the mkimage timestamp.
The Boot and Recovery U-Boot images are built in `crux-uboot/tools/crux/`.

## Tests

`test-ram-boot-pe13.py` and `test-b-host-profile.py` run on the Mac.
`test-local-build-state.py` needs the VM's Btrfs filesystem and non-interactive
mount-namespace sudo:

```sh
orb -m cruxbuild env TMPDIR=/home/qingli \
  NINJA=/home/qingli/crux-pe13-offline-2026-09-25/pe13/prebuilts/build-tools/linux-x86/bin/ninja \
  PYTHONDONTWRITEBYTECODE=1 python3 \
  /mnt/mac/Users/qingli/Documents/project/crux/crux-pe13-distributed/local/test-local-build-state.py
```
