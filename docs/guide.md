# Crux PE13: installing and building

This guide is for anyone with a Xiaomi Mi 9 Pro 5G (crux) who wants to install
this PixelExperience 13 port, update it, or build it from the public
repositories. The pipeline internals are in the [repository README](../README.md)
and the [operation guide](standard-runner-freeze.md).

Versions are not repeated here: the pinned install TWRP is
[`config/install-twrp.json`](../config/install-twrp.json), the source lock is
[`config/cloud-source-manifest.json`](../config/cloud-source-manifest.json), and
every ROM release names its own build, fingerprint and install TWRP profile in
the `README.txt` it carries.

## Before you start

- **This is a development build** (`aosp_crux-userdebug`). ADB is on by default
  without RSA host authentication: any computer on USB gets a root shell. Do not
  use it on a phone that holds personal data.
- **No rescue is provided.** This guide covers building, the first install,
  updates and rollback. Going back to MIUI, recovering a phone that no longer
  reaches its bootloader, or any EDL procedure is outside its scope.
- **Every install is a clean install.** It formats the PE data and metadata
  volumes; there is no data-preserving update.
- The bootloader must be unlocked the official way.
- Before the first install, back up this phone's own partitions (step 4 below).
  They hold the baseband EFS (IMEI) and calibration data, and nothing else can
  restore them.

## What gets installed

ABL, the phone's own bootloader, only starts the `boot` and `recovery`
partitions, so both hold a U-Boot:

- **Boot U-Boot** (`boot`): starts PE on a normal power-on and supplies the ABL
  device identity to it.
- **Recovery container U-Boot** (`recovery`): reached with `reboot recovery`. It
  lands a staged install and offers a fastboot that boots the install TWRP.
- **Install TWRP**: runs only in RAM, never written to a partition. Its PID 1
  storage guard leaves only the four PE partitions writable. On a LUN0 table it
  does not recognise (such as the official MIUI table) it makes every UFS block
  device read-only and the installer refuses to run; adb still works, so the
  table can be read and partitions backed up.
- **PE ROM**: a signed `crux-install/1` ZIP that writes `pesystem` and `pevendor`
  and stages two FITs that the container lands into `cache`.

PE needs its own partitions (`pemetadata`, `pesystem`, `pevendor`,
`peuserdata`) on LUN0. The first install rewrites the official LUN0 table once:
MIUI's `cust` entry is removed, `vendor`, `system` and `userdata` become 16 MiB
name stubs, and the PE partitions take the rest of the disk. MIUI no longer
boots afterwards.

All published files:

| File | Where |
| --- | --- |
| Signed ROM ZIP, its `.sha256` and `README.txt` | prereleases `crux-pe13-<build>-v<n>` of this repository |
| Install TWRP `twrp-b-install.itb` and its `.sha256` | the tool Release named by `release_tag` in `config/install-twrp.json`; every ROM prerelease also attaches the one its manifest accepts |
| `crux-uboot-boot.img`, `crux-uboot-recovery.img` | prereleases `crux-uboot-<commit>` of [coachpo/crux-uboot](https://github.com/coachpo/crux-uboot/releases) |
| Official `vbmeta.img` | the official MIUI fastboot package for crux (Xiaomi CDN) |

## What you need

- A crux with an unlocked bootloader, running official MIUI. LUN0 must hold the
  official MIUI table. The table tools refuse any other table, and changing
  partition tables in other ways is out of scope.
- A computer with Android platform-tools (`adb`, `fastboot`), Python 3, and a
  checkout of this repository (`local/` holds the table tools).
- A serial terminal for the U-Boot console. Each U-Boot also exposes a USB
  CDC-ACM console (USB ID `18d1:4ee0`; `/dev/ttyACM*` on Linux,
  `/dev/cu.usbmodem*` on macOS). [`local/uconsole_watch.py`](../local/uconsole_watch.py)
  keeps one open across reboots. On the phone, the U-Boot menu moves with
  Volume Down and selects with Power; Volume Up does nothing in U-Boot.

## First install

These steps were run end to end on a crux on 2026-11-25, from the official MIUI
table to a running PE. The exception is the key combination in step 3, which
is untested.

1. **Prepare the four partition images** on the computer:

   ```sh
   head -c 33554432 /dev/zero > dtbo-zero.img     # dtbo must be all zero, or ABL skips U-Boot
   cp <MIUI package>/images/vbmeta.img vbmeta-disabled.img
   printf '\x00\x00\x00\x03' | dd of=vbmeta-disabled.img bs=1 seek=120 conv=notrunc   # flags 3: no verity/verification
   shasum -a 256 crux-uboot-boot.img crux-uboot-recovery.img twrp-b-install.itb crux-pe13-<build>.zip
   ```

   Compare the hashes with the `SHA256SUMS` of the `crux-uboot` release (its
   `boot/boot.img` and `recovery/recovery.img` lines) and with the `.sha256`
   files of the other two.

2. **Flash them from ABL fastboot.** Power on with Volume Down and Power held
   until FASTBOOT shows, then:

   ```sh
   fastboot getvar product      # crux
   fastboot flash dtbo dtbo-zero.img
   fastboot flash vbmeta vbmeta-disabled.img
   fastboot flash boot crux-uboot-boot.img
   fastboot flash recovery crux-uboot-recovery.img
   fastboot reboot
   ```

   Do not let MIUI start after this: it may overwrite the Recovery partition.
   `fastboot reboot` starts the Boot U-Boot, which waits in its menu (there is
   no PE yet).

3. **Start the install TWRP in RAM from the Boot U-Boot.** On the U-Boot
   console, pick *Drop to shell* and enter `run usb_fastboot`. On the computer:

   ```sh
   fastboot stage twrp-b-install.itb
   fastboot continue
   ```

   The console comes back to the U-Boot prompt; enter one line:

   ```
   setenv fdt_high; setenv initrd_high; setenv bootargs; bootm start 0xC0000000; bootm loados; bootm ramdisk; if crux_install bootargs; then bootm prep; run boot_go; fi
   ```

   `boot_go` arms a 30 s hardware watchdog before handing over, so a kernel that
   hangs resets the phone. ABL's `fastboot reboot recovery` does **not** reach
   Recovery on crux. Holding Volume Up and Power at power-on is the usual way
   into Recovery, but it is untested here.

4. **Back up and read the table** in that TWRP. On the official table it runs
   read-only: `adb shell ls /crux-ram-twrp-guard.readonly` exists and
   `/crux-ram-twrp-guard.ready` does not.

   ```sh
   for n in persist persistbak modemst1 modemst2 fsg fsc; do
     adb exec-out "dd if=$(adb shell readlink -f /dev/block/by-name/$n | tr -d '\r') bs=1M 2>/dev/null" > $n.img
   done
   adb exec-out "dd if=/dev/block/sda bs=4096 count=8 2>/dev/null" > sda-head-8x4k.bin
   last=$(python3 -c "import struct;print(struct.unpack_from('<Q',open('sda-head-8x4k.bin','rb').read(),4096+48)[0])")
   adb exec-out "dd if=/dev/block/sda bs=4096 skip=$last count=6 2>/dev/null" > sda-tail-6x4k.bin
   ```

   Keep `2>/dev/null` inside the quoted device command, or dd's statistics end
   up in the image. Compare each image's SHA-256 with `adb shell sha256sum` of
   the same node before trusting it. Keep these backups somewhere safe; they
   belong to this phone only.

5. **Derive the PE table and go to the container:**

   ```sh
   python3 local/lun0-gpt.py --head sda-head-8x4k.bin --tail sda-tail-6x4k.bin --out gpt
   adb reboot recovery
   ```

   `lun0-gpt.py` must report `source: official`. `adb reboot recovery` from TWRP
   makes ABL start the Recovery container, which prints
   `LUN0 layout differs; nothing to land` and waits in its menu.

6. **Write the table from the container's fastboot.** Pick *Enable fastboot
   mode*, then:

   ```sh
   python3 local/write-lun0-gpt.py --head sda-head-8x4k.bin --tail sda-tail-6x4k.bin \
     --gpt gpt --work gpt-write --run
   ```

   It first checks that the phone still holds exactly the captured table. It then
   writes only the changed blocks (the primary header and entries, and the
   backup entries and header), reads every written run back, and stops at the
   first failure. Without `--run` it only prints the fastboot commands.

7. **Install PE in the same fastboot session:**

   ```sh
   fastboot boot twrp-b-install.itb
   adb shell ls /crux-ram-twrp-guard.ready          # the guard now recognises the table
   adb push crux-pe13-<build>.zip /tmp/
   ```

   In TWRP: *Install*, go up to `/tmp`, pick the ZIP, swipe, and confirm with
   Volume Up when the installer lists its write targets. It then reboots into the
   container, which lands the FITs and starts PE. The first boot takes a few
   minutes.

## Updating and rolling back

From a running PE, follow the ROM ZIP's `README.txt`:

```sh
adb reboot recovery                 # container menu: Enable fastboot mode
fastboot boot twrp-b-install.itb    # the install TWRP that ZIP's manifest accepts
adb push crux-pe13-<build>.zip /tmp/
```

Then install it in TWRP as above. A ZIP names the install TWRP profiles it
accepts (`twrp_profiles` in its `crux-install.txt`, also listed in its
`README.txt`), and the installer refuses any other. Use the install TWRP
attached to the same release.

Rolling back is the same clean install with an older release ZIP. Keep the ZIP
and the install TWRP of a build you know works.

If the Boot U-Boot no longer starts, ABL is expected to stay in its own
fastboot (untested); enter it with Volume Down and Power, and flash
`crux-uboot-boot.img` or `crux-uboot-recovery.img` from a `crux-uboot` release
again.

## What has been tested

Device acceptance runs on one development phone. A pass covers only what it
lists, for the build it names:

- Signed ZIP clean install, FIT landing with readback CRCs, PE partition routing,
  FBE v2 and vold, and 300 s on one boot ID (`local/verify-pe-rom.py`).
- Boot with the ABL identity from both U-Boots. Credential-encrypted storage
  across a cold reset and an ordinary reboot. ART, keystore2 and Keymaster 4.0
  at runtime. SELinux denials compared against a reference build.
- Fingerprint enrolment and unlock, and the ambient light sensor, confirmed by
  hand. Since the display fix only 60 Hz remains.
- The first install above, including the install TWRP's read-only mode on the
  official table.

**Not tested** on current builds: calls, SIM, mobile data, IMS and 5G
registration, Wi-Fi, camera, audio, vibration, charging and real power use,
suspend with the offline RTC, and most of the UI. TWRP's own UI beyond flashing,
MTP, decryption and backup/restore are not tested either.

## Building

There are three products, each built reproducibly from public source:

| Product | Repository | Build |
| --- | --- | --- |
| PE ROM ZIP | this repository | GitHub Actions on standard runners (path A), or locally (path B) |
| U-Boot Boot image and Recovery container | [coachpo/crux-uboot](https://github.com/coachpo/crux-uboot) | `tools/crux/build-images.sh` on Ubuntu 22.04 x86_64 or its workflow; CI and VM builds are byte-identical ([tools/crux/README.md](https://github.com/coachpo/crux-uboot/blob/master/tools/crux/README.md)) |
| TWRP FIT (input of the install TWRP) | [coachpo/android_device_xiaomi_crux_twrp](https://github.com/coachpo/android_device_xiaomi_crux_twrp) | its scripts or workflow; CI and VM builds are byte-identical |

The install TWRP is derived from that TWRP FIT with
[`local/build-b-twrp.py`](../local/README.md#ram-only-install-twrp).

### The two snapshot sources

Two of the 1,170 locked projects, `vendor/gms` and `external/faceunlock`, are
read from private snapshot repositories that only this repository's workflow can
access. Their original source is the PixelExperience GitLab bundle collection on
Archive.org (`gitlab.pixelexperience.org_bundles_20240412`). Obtain them
yourself. The lock entry of each one names the commit (`revision`) and its Git
tree (`expected_tree`); both must match whatever you use. An archive you make
yourself has a different SHA-256 from the private snapshot's; that is expected.
No tool here fetches them for you.

### Path A in a fork

Path A runs unattended on free standard `ubuntu-22.04` runners of a public
repository; a cold chain takes about 5–6.5 hours. To run it in a fork:

- Put the two snapshot sources (above) in **private** repositories of yours,
  each as a Release asset made with `git archive` of the locked commit (the
  fetcher refuses a public snapshot repository). In the lock, point each
  project's `url` at `https://github.com/<owner>/<repository>` and its
  `release_snapshot` at that repository, Release and asset (IDs, tag, name, size,
  SHA-256); the commit and tree stay as they are. Set the secret
  `CRUX_SOURCE_SNAPSHOTS_TOKEN` to a token that can read those repositories.
- Make your own install key: a P-256 key in PEM form, for example
  `openssl ecparam -name prime256v1 -genkey -noout -out install.pem`. Its public
  half goes into `keys/` as PEM and as raw 64-byte X‖Y
  (`openssl ec -in install.pem -pubout -outform DER | tail -c 64`).
- Store the private key as the secret `CRUX_INSTALL_SIGNING_KEY` of an
  environment named `install-signing`, limited to `main`.
- Derive an install TWRP with your public key (`local/build-b-twrp.py`) and
  publish it as a tool Release in your fork. Point `config/install-twrp.json`
  and the public-key paths (`PUBLIC_KEY` in `scripts/release_sign.py` and
  `scripts/release_zip.py`) at your files. ZIPs signed with your key install only with that
  install TWRP.
- Enable Actions in the fork. The workflows and scripts take the repository
  from the running workflow (`GITHUB_REPOSITORY`), so the chain, its native
  reuse cache and its Releases stay inside the fork. Signing, publishing and the
  snapshot layers still run only on `main`.

### Path B on a Mac

Path B is supported only on an Apple Silicon Mac with an OrbStack Ubuntu 22.04
amd64 VM (Rosetta), about 48 GiB of VM memory and a Btrfs filesystem for the
build cache. A native x86_64 Ubuntu host is not a supported or tested B host:
the scripts drive the VM with `orb`, package on the Mac with a Mac-native
`mkimage`, and always apply the Rosetta host profile (dexpreopt off). B compiles
the same locked source as A and packages the same signed ZIP. See
[`local/README.md`](../local/README.md) for the commands; a cold build takes
about 2 h 40 min.

B reads a Git checkout of every locked project at its pinned revision
(`local/check_local_source.py` checks each HEAD). On a new VM,
[`local/checkout-from-lock.py`](../local/README.md#b-build) creates it from the
lock: shallow fetches by commit from googlesource, GitHub and GitLab, the kernel
as its own checkout linked into the tree, the lock's layout, and the two snapshot
projects from the Git bundles or repositories you pass with `--snapshot`. Then
`build-pe13-local.sh` without `--resume` builds a cold managed cache.

Packaging on the Mac needs Homebrew's `dtc` and `zstd`, the macOS
`aarch64-unknown-linux-gnu` cross toolchain and a Mac-native `mkimage` that you
build from a `crux-uboot` checkout next to this repository; the
[Mac packaging tools](../local/README.md#mac-packaging-tools) list the commands.

## Pins in one place

| What | File |
| --- | --- |
| Every source project, its revision and the snapshot identities | `config/cloud-source-manifest.json` |
| The install TWRP every new ZIP accepts and carries | `config/install-twrp.json` |
| The public key that verifies every signature | `keys/` |
| Install TWRP derivation input (twrp-crux FIT) | `SOURCE_FIT_SHA256` in `local/build-b-twrp.py` |
