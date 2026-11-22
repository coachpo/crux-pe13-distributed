# Distributed Crux PixelExperience 13 development build

This repository owns the GitHub Actions pipeline for the **development** Crux
PE13 product (`aosp_crux-userdebug`): four development images, two U-Boot FITs
and the signed TWRP ZIP made from them. The local OrbStack build compiles the
same pinned source and packages the same ZIP with the same tools, but does not
publish here; its host tools are in [`local/`](local/README.md).

## Standard-runner build chain

`nightly.yml` checks the sources every night at 23:37 UTC. When the configured
Android 13 maintenance branch of a tracked fork or the commit recorded by a
snapshot repository moved, or `main` changed `scripts/`, `config/`,
`installer/`, `keys/`, `.github/` or `references/pe13-boot-contracts/` since
the last full freeze, it takes the next CI build number of the Helsinki day
(`YYYYMMDD50` to `YYYYMMDD99`; local builds use `01` to `49`), pins the
sources, regenerates the device marker patch and its patched tree, commits as
`github-actions[bot]` and dispatches a full freeze. A night without changes, or
with a chain run still queued or running, starts nothing. Each stage then
dispatches the next one from its last job; partial runs (`source-only`,
`index`, `shard`) do not continue the chain. Build identities and upstream run
provenance are recorded with the
[published ZIP releases](https://github.com/coachpo/crux-pe13-distributed/releases).
The entire path uses public-repository standard `ubuntu-22.04` runners.

Manual dispatch of `nightly.yml` forces a complete build even with no changes,
but does not start a duplicate while another chain is active. It allocates the
build number and marker just like the scheduled entry. `scripts/nightly.py`'s
`TRACKED` table names the explicit branches of the ten maintained ROM forks:
device, vendor blobs, kernel, recovery, frameworks, ART, display, system/core,
ADB and vendor/aosp. Make is fixed at official `0189aa1e52e4461d6bfd9976f6d602e23738b34c`
and build-tools at official `7d311e630d48dcd46e6036e34e4d588bd9c064c9`, with
their expected trees. Other upstream projects stay pinned; the two private
snapshot inputs keep their separate identity checks. A repository
default-branch change never selects another Android version implicitly.

The official build-tools pin supplies nsjail to A's Soong host probe; graph
receipts record actual sandbox observations. A uses the official full
dexpreopt/profile defaults, and freeze verifies the generated configuration.
B's Rosetta nsjail and dexpreopt workaround lives only in its private source
overlay (`local/pe13-b-host-profile.py`). Delivery uses `crux_zip.py`; Android
target-files re-signing is not a Crux interface.

Before cloud acceptance of code changes, run the
[isolated local cold preflight](local_replay/README.md): targeted graph/contract
checks, real cold high-risk tasks and complete unsigned packaging, expanding
the predecessor chain when needed. This maintenance check is not an unattended
nightly runtime dependency. A remains independent of the Mac and OrbStack;
LOCAL diagnostic outputs must never become A build or publication inputs.

| Stage | Maintained inputs and handoff |
| --- | --- |
| Prepare | `freeze-standard.yml` uses `config/cloud-source-manifest.json` and its pinned projects, patches and toolchains to create a fresh Soong/Kati graph. `prepare-standard.yml` indexes, slices and audits the cold input closure without compressing a capsule for every shard. The registry binds the current plan and each audited input descriptor. |
| Compile | `build-standard.yml` admits the current graph/index/registry and creates `cloud-build-context`. Actual dependency waves, admitted against `config/cloud-sharding-profile.json`, resolve each task only after its real predecessor outputs are known. A verified hit is adopted with original native provenance and a current-execution receipt; only a miss materializes a verified current capsule and runs native compilation. The summary validates every declared workflow slot, current receipt and original native output binding. |
| Assemble and verify | `assemble-standard.yml` consumes the context, `cloud-fresh-build-summary` and plan-owned same-run producer bindings. It verifies the four images and both identity-free FITs, writes `delivery-record.json` and `SHA256SUMS`, checks that the compressed system payload decodes to the native system image identity, then builds `installer/` and writes the unsigned ZIP payload (`zip-payload/`) with `scripts/crux_zip.py prepare`. `cloud-fresh-rom-<build_run>` is an Actions artifact, not a release; `zip-manifest` holds the payload's `crux-install.txt` for signing. |
| Release | The `sign` job runs in the `install-signing` environment: it waits for the assembly run to finish, checks its lineage and the manifest identity and signs `crux-install.txt` with the `CRUX_INSTALL_SIGNING_KEY` secret. The `release` job verifies the whole assembly artifact, builds the ZIP, checks the signature against `keys/crux-b-install-2026-11-07.pub.pem` and stages, then publishes, the prerelease `crux-pe13-<build>-v<n>`: this assembly's tag, or the next free version of its build. Manual dispatches keep `preflight` (read only), `draft` and `publish`, and accept an offline signature in place of the CI one. |

A failed stage, dispatch or nightly check opens the issue `PE13 cloud build
<build> failed`, assigned to the owner, or comments on it if it is open. Re-run
the failed jobs on the same commit. Every consuming stage checks that its
upstream runs used its execution commit before consuming inputs or signing.
If pipeline code changes, start a new full freeze (manual nightly is the unified
entry); do not continue new code against older prepared stage artifacts.
Verified source-layer reuse is governed by its byte contracts.

The execution digest covers `scripts/*.py`, `.github/workflows/*.yml`,
`.github/actions/**` and `config/*.json` except the source lock: changing any of
them makes every native task a miss on the next chain, a cold compile of about
5–6.5 hours. Keep tools that A does not run out of those paths.

## Native-shard reuse

Native reuse is content-addressed, not accepted because Git SHAs or task names
match. Keys bind audited source bytes and resolved graph, execution policy,
host/runtime identity and exact predecessor output content. Adoption preserves
the original native producer; it does not relabel old bytes as newly compiled.
`force_cold=true` on manual nightly disables native adoption for the whole new
chain without clearing any cache or disabling verified source-layer reuse.
Kernel owners always miss because the kernel embeds date/user/hostname outside
the declared Ninja inputs.

The partition profile isolates build-identity outputs, the atomic kernel owner
and final image packaging from ordinary compilation. Exact recovery-resource,
installed-files and recovery-patch outputs keep those packaging helpers with
their image dependency chain. Ordinary Java, native and helper actions may share
a task to preserve real tool-bootstrap chains. Units are ordered by stable
primary output paths. The profile's wave capacity declares serial
reusable-workflow slots in the **same** compile execution; unused slots perform
no native work. Regenerate their declarations with
`python3 scripts/cloud_schedule.py --write-workflow` after an intentional policy
change. Missing, duplicate, empty, over-capacity or dependency-inverted actual
waves fail admission.

Recovery tree assembly, CPIO, compressed ramdisk, Recovery image and resource
archive form one native packing unit; the kernel trio stays a separate atomic
unit. Filesystem construction and installed file-list actions are image work,
not ordinary compilation.

Only a typed compile/assembly/preflight content-validation failure after actual
reuse qualifies for one same-pin cold retry. Transport, signing, publication and
cancellation do not. Forced-cold executions cannot loop into another automatic
retry. See the [operation guide](docs/standard-runner-freeze.md) for stage
recovery, input reuse, expiry rules and release checks.

## The ZIP and its installation

A release carries `crux-pe13-<build>.zip`, its `.sha256`, `README.txt` and the
pinned RAM-only install TWRP (`config/install-twrp.json`) with its `.sha256`.
The ZIP is a crux-install/1 clean install: the update-binary checks the
signature with the TWRP's key, the device serial (`any` for these identity-free
FITs), the Recovery container U-Boot and the live partition layout before it
writes `pesystem`/`pevendor`, formats the PE data and metadata volumes and stages
both FITs for U-Boot to land. Neither the FITs nor the images carry the
development phone's identity or host paths: the boot contract has no identity
tokens, `package-pe13.py` refuses any, U-Boot injects them from ABL at boot, and
assembly runs `crux_zip.py prepare --forbid-text` for the owner's home paths.
Follow the ZIP's `README.txt`: boot the install
TWRP with `fastboot boot` from the Recovery container's menu, push the ZIP and
install it in TWRP.

## Checks

Source/graph/preparation and assembly/signing-manifest artifacts are retained
for 3 days, source failure evidence 7 days, native shards/receipts and compile
context/summary 30 days, maintenance fixtures 1 day; the repository default is
90 days. These windows bound retries and re-publication; see the
[retention details](docs/standard-runner-freeze.md#acceptance-evidence-and-artifact-retention).

```sh
python3 -m unittest discover -s tests
make -C installer test
```

`test-artifact-retention.yml` tests the production `retain-artifact` helper's
retry/failure contract; it produces no compilation inputs. Production stages
collect their own runner resource samples.
