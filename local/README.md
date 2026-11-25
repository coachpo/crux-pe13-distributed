# Local B build, packaging and device tools

These host tools build path B in OrbStack, package it as the same signed
crux-install/1 TWRP ZIP that the Actions chain publishes, and reach the U-Boot
console. No workflow uses this directory, and LOCAL outputs are never A build
or publication inputs. Every device step needs an exclusive window from the
coordinator the owner designates and its own write authorization; every debug
kernel handoff must arm and check the U-Boot watchdog.

Paths below are relative to the workspace root, and the commands run from there.
`M=/mnt/mac$PWD` is the same directory as the VM sees it, and
`H=$(orb -m cruxbuild printenv HOME)` is the VM build user's home.
Host inputs outside the repositories live in `~/Crux-Inputs/` (deployed U-Boot
images) and `~/Crux-Rescue/` (rescue material); the B install
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
   M=/mnt/mac$PWD H=$(orb -m cruxbuild printenv HOME)
   orb -m cruxbuild python3 $M/crux-pe13-distributed/local/check_local_source.py \
     --tree $H/crux-pe13-offline-2026-09-25/pe13
   ```

2. Build with `build-pe13-local.sh`. Resume the one managed cache with a new
   10-digit build number (B uses `NN` 01–49) and a new evidence directory:

   ```sh
   M=/mnt/mac$PWD H=$(orb -m cruxbuild printenv HOME)
   orb -m cruxbuild bash $M/crux-pe13-distributed/local/build-pe13-local.sh --resume <BUILD> \
     $H/crux-pe13-b-2026112103 $M/out/<evidence> crux-pe13-local-<BUILD> > <log> 2>&1
   ```

   - `--resume` accepts only a helper-managed cache. `--seed-from STATE` starts
     a separate managed cache from an existing one with mandatory Btrfs
     reflinks; with no option the helper starts a cold cache.
   - Each run gets fresh private source/kernel overlays. `pe13-b-host-profile.py`
     applies the B-only Rosetta settings there (dexpreopt/profile off,
     userdebug check downgraded to a warning, nsjail hidden); the shared tree
     keeps the official contents. The profile refuses unknown fragments.
   - The build sees the checkouts only at neutral paths: overlays of the PE tree
     and the kernel at `/srv/crux/crux-pe13-offline-2026-09-25/pe13` and
     `/srv/crux/crux-kernel-cepheus`, and the cache's Ninja graph names the
     logical OUT `/srv/crux/android-out`, onto which a private bind mount maps
     the cache's `android-out`. Keep those three (empty) directories: a cache
     with another logical path means a cold build. `BUILD_USERNAME=crux` and
     `BUILD_HOSTNAME=build` keep the host user out of the images. Do not run
     another Android build concurrently.
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
   packages the identity-free FITs with `references/pe13-boot-contracts/package-pe13.py`
   (its boot contract carries no device identity), validates the images (PE fstab routing, native guard,
   development ADB properties) and writes the unsigned payload with
   `scripts/crux_zip.py prepare`. Keep the Mac-native Crux `mkimage` at
   `crux-uboot/.output-crux/xiaomi-crux/tools/mkimage`, or select it with
   `MKIMAGE`. The U-Boot candidate builder now runs on Linux and its host
   tool is not a replacement for the Mac executable.

4. Sign with the B install private key and finalize:

   ```sh
   python3 crux-pe13-distributed/scripts/crux_zip.py sign --payload <payload> --key ~/.crux-keys/crux-b-install-2026-11-07.pem
   python3 crux-pe13-distributed/scripts/crux_zip.py finalize --payload <payload> \
     --public-key crux-pe13-distributed/keys/crux-b-install-2026-11-07.pub.pem --out <dir>
   ```

## Install

The signed ZIP is the only install entry. It formats `peuserdata` and
`pemetadata`; there is no data-preserving install. It never writes Boot, dtbo,
the GPT name stubs, the unused cache slots or the GPT.

From the Recovery-container U-Boot menu choose *Enable fastboot mode*,
`fastboot boot` the RAM-only install TWRP v6 (`config/install-twrp.json` pins
it), `adb push` the ZIP to `/tmp`, install it in TWRP and confirm with Volume
Up. The installer checks the signature, serial (`any`), container U-Boot and
partition layout, writes `pesystem`/`pevendor`, stages both FITs and reboots;
the container lands them over the full Recovery `0x38000 × 0x3b00` and ROM
`0x3c000 × 0x3100` windows and starts PE with the ABL identity.

After the first boot, `verify-pe-rom.py --identity <evidence>/image-validation.json
--marker <marker> --out <new dir>` checks over root ROM ADB, read-only and for at
least 300 s, the build identity, the PE partition routing, vold/CE state and the
absence of zygote failures.

## LUN0 GPT

`lun0-gpt.py --head H --tail T --out D` derives the current LUN0 table from a
read-only capture of LBA 0–7 and of the last six blocks. It accepts the official
table the MIUI EDL package leaves (`gpt_main0.bin` with `patch0.xml`: 34 entries,
`userdata` up to the last usable LBA), so an EDL restore needs no earlier layout,
and the former 38-entry PE-only table. MIUI's `cust` (entry 31) is removed,
`vendor`, `system` and `userdata` become 16 MiB name stubs at `0x60000`/`0x61000`/
`0x62000` (ABL and the PE guard look them up by name), and the PE partitions are
entries 35–38: `pemetadata` `0x63000`, `pesystem` `0x64000`, `pevendor`
`0x164000`, and `peuserdata` (`0x1d4000`, `0x3982e00` blocks) up to the end of the
disk. These extents are the installer's `volumes[]` and the U-Boot
`CRUX_PESYSTEM_LBA`. Existing entries keep their type, unique GUID and attributes;
PE entries created from the official table get GUIDs derived from their names.
Entry numbers of the remaining partitions do not change. It writes head/tail
images of the capture's shape and `layout.json` with the source kind, the changed
blocks and their digests; no device access. From the official table the changed
blocks are `0x1`–`0x3` and the backup's entry array and header.

## Keyless entry to the U-Boot console

1. Confirm the assigned serial and running fingerprint over USB ADB, check that
   no other watcher owns the serial port or `/tmp/crux-m0`, and start one watcher:

   ```sh
   mkdir -p /tmp/crux-m0 && : > /tmp/crux-m0/cmd.txt
   ~/.venvs/crux-local/bin/python crux-pe13-distributed/local/uconsole_watch.py
   ```

   The watcher opens only the CDC-ACM port whose USB identity is the Crux U-Boot
   gadget (`18d1:4ee0`), so other ACM devices such as a USB camera are ignored;
   `--port /dev/cu.usbmodemNNNN` names one node instead.

2. `adb -s "$SERIAL" reboot` (an ordinary reboot). When the menu appears on USB
   CDC-ACM, write the **observed** *Drop to shell* key to `/tmp/crux-m0/cmd.txt`
   and wait for `=>`. `@menu-redraw` asks the watcher to send two raw Ctrl-N
   bytes so a late console can see the menu; do not guess keys.
3. Read `version`, `printenv bootdelay menucmd boot_go kernel_watchdog_ms
   bootmenu_0 bootmenu_1 bootmenu_2 bootmenu_3 crux_uboot_role
   crux_boot_pe_rom` and `scsi part`. Menu block counts are read capacities, not
   write ranges.
4. Return with `run crux_boot_pe_rom` (it injects the ABL identity; `run
   boot_pe_rom` keeps the FIT's bootargs). Confirm the serial, fingerprint, a
   new boot ID and `sys.boot_completed=1`, stop the watcher and report the
   endpoint to the coordinator.

If the expected menu or endpoint does not appear, stop and report it.

`ram-boot-pe13.py --fit F --fit-sha256 H --mode recovery|rom --expected-uboot-version V --out D`
(`ANDROID_SERIAL` names the phone, `V` is the deployed Boot U-Boot banner) stages a
FIT into RAM from that shell with its final DT/bootargs and the checked watchdog
for kernel debugging (`--dry-run` validates without the device); completion
needs a unique executed marker, not command echo.

## RAM-only install TWRP

`build-b-twrp.py` derives the install TWRP from the public twrp-crux FIT
`twrp-crux-3.7.1_12.itb` (`2df4c8d6…`, twrp-crux `e9a39b6` with kernel
`2900b2cc1af5`; its VM cold build and Actions run 45329517224 give the same
bytes). Its DT carries no device identity; the script refuses a source whose
bootargs hold an ABL identity token. Kernel and DT are kept; the ramdisk gets
the static build of `ram_twrp_preinit.c` as `/init` (the PID 1 storage guard),
no mount routes except the in-RAM BCB sink, the
bounded recovery capabilities, no Qualcomm decryption services, TWRP's default
RAM folder, the zip-flash slider settings, the B public key (raw 64-byte form in
`keys/`) and a new profile ID. `ram_twrp_pack.py` holds its FIT and CPIO
helpers; `test-build-b-twrp.py` covers the ramdisk and DT checks. Rerun it only
when the B key, TWRP or the PE extents change and publish the result as a new
tool Release, then update `config/install-twrp.json`.

The guard decides from sysfs before any ioctl. When the named partitions sit on
their nodes and the four PE targets match one known LUN0 layout (the former
PE-only table or the current table of `lun0-gpt.py`, the installer's
`volumes[]`), it makes the ten originals read-only, keeps the PE targets
writable and writes `/crux-ram-twrp-guard.ready`; on either table its adb can
restore the other. On any other table, such as the official MIUI GPT before a
first install, it waits for the six UFS LUNs, makes every UFS block node (whole
LUNs and all partitions) read-only, writes `/crux-ram-twrp-guard.readonly` and
no ready receipt, so the installer refuses while adb can read the GPT and back
up partitions. TWRP starts in both modes; only missing LUNs or malformed sysfs
restart the phone. Build it from the workspace root:

```sh
aarch64-unknown-linux-gnu-gcc -Os -static -s -o <dir>/preinit.elf \
  crux-pe13-distributed/local/ram_twrp_preinit.c
python3 crux-pe13-distributed/local/build-b-twrp.py --preinit <dir>/preinit.elf \
  --source-fit <twrp-crux dist>/twrp-crux-3.7.1_12.itb \
  --public-key crux-pe13-distributed/keys/crux-b-install-2026-11-07.p256.pub \
  --mkimage crux-uboot/.output-crux/xiaomi-crux/tools/mkimage --out <new dir>
```

The profile ID and the kernel, DT and ramdisk components are deterministic; the
FIT bytes carry the mkimage timestamp. With GCC 15.2.0 the guard source gives
preinit `b12ae87f…`. The pinned v6 (`f058a265…`, preinit `5ea73cb8…` without the
read-only mode) was derived from an earlier RAM TWRP that is not public; this
script no longer reproduces it.
The Boot and Recovery U-Boot images are built in `crux-uboot/tools/crux/`.

## Tests

`test-ram-boot-pe13.py`, `test-lun0-gpt.py`, `test-b-host-profile.py` and
`test-build-b-twrp.py` run on the Mac. `test-ram-twrp-preinit.py` builds the
guard with `-DCRUX_GUARD_TEST` against a synthetic sysfs and needs Linux, so run
it in the VM (`orb -m cruxbuild bash -lc "cd /tmp && python3
$M/crux-pe13-distributed/local/test-ram-twrp-preinit.py"`).
`test-local-build-state.py` needs the VM's Btrfs filesystem and non-interactive
mount-namespace sudo:

```sh
M=/mnt/mac$PWD H=$(orb -m cruxbuild printenv HOME)
orb -m cruxbuild env TMPDIR=$H \
  NINJA=$H/crux-pe13-offline-2026-09-25/pe13/prebuilts/build-tools/linux-x86/bin/ninja \
  PYTHONDONTWRITEBYTECODE=1 python3 \
  $M/crux-pe13-distributed/local/test-local-build-state.py
```
