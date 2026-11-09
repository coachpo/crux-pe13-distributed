# Distributed Crux PixelExperience 13 development build

This repository owns the GitHub Actions pipeline for the **development** Crux
PE13 product (`aosp_crux-userdebug`): four development images, two U-Boot FITs
and the signed TWRP ZIP made from them. The local OrbStack build compiles the
same pinned source and packages the same ZIP with the same tools, but does not
publish here.

## Standard-runner build chain

`nightly.yml` checks the sources every night at 23:37 UTC (01:37 in Helsinki in
winter). When the default-branch head of a tracked fork (device, kernel,
recovery, `frameworks/base`, `art`, the CAF display HAL) or the commit recorded
by a snapshot repository moved, or `main` changed `scripts/`, `config/`,
`installer/`, `keys/`, `manifests/` or `.github/` since the last full freeze, it
takes the next CI build number of the Helsinki day (`YYYYMMDD50` to
`YYYYMMDD99`; local builds use `01` to `49`), pins the sources, regenerates the
device marker patch and its patched tree, commits as `github-actions[bot]` and
dispatches a full freeze. A night without changes, or with a chain run still
queued or running, starts nothing. Each stage then dispatches the next one from
its last job; partial runs (`source-only`, `index`, `shard`) do not continue the
chain. The last manually released build is `2026110804`
([crux-pe13-2026110804-v1](https://github.com/coachpo/crux-pe13-distributed/releases/tag/crux-pe13-2026110804-v1),
from [assembly 42553763990](https://github.com/coachpo/crux-pe13-distributed/actions/runs/42553763990)).
The entire path uses public-repository standard `ubuntu-22.04` runners.

| Stage | Maintained inputs and handoff |
| --- | --- |
| [Prepare](docs/standard-runner-freeze.md) | `freeze-standard.yml` uses `config/cloud-source-manifest.json` and its pinned projects, patches and toolchains to create a fresh Soong/Kati graph. `prepare-standard.yml` indexes, slices and packs it into a shard plan, a pack registry and one cold capsule per shard. |
| Compile | `build-standard.yml` admits the pack registry of the preparation run it is given. The graph receipt, the canonical index receipt and the slice plan come from the freeze and preparation runs that registry names and must bind to each other; with the runtime policy they form `cloud-build-context`, which drives eight calls to `compile-standard-wave.yml`. Shards import only declared predecessor outputs from this execution; the final summary verifies a fresh receipt for every admitted shard. |
| Assemble and verify | `assemble-standard.yml` consumes the context, `cloud-fresh-build-summary` and seven same-run producer bindings. It verifies the four images and both identity-free FITs, writes `delivery-record.json` and `SHA256SUMS`, checks that the compressed system payload decodes to the native system image identity, then builds `installer/` and writes the unsigned ZIP payload (`zip-payload/`) with `scripts/crux_zip.py prepare`. `cloud-fresh-rom-<build_run>` is an Actions artifact, not a release; `zip-manifest` holds the payload's `crux-install.txt` for signing. |
| Release | The `sign` job runs in the `install-signing` environment: it waits for the assembly run to finish, checks its lineage and the manifest identity and signs `crux-install.txt` with the `CRUX_INSTALL_SIGNING_KEY` secret. The `release` job verifies the whole assembly artifact, builds the ZIP, checks the signature against `keys/crux-b-install-2026-11-07.pub.pem` and stages, then publishes, the prerelease `crux-pe13-<build>-v<n>`: this assembly's tag, or the next free version of its build. Manual dispatches keep `preflight` (read only), `draft` and `publish`, and accept an offline signature in place of the CI one. |

A failed stage, dispatch or nightly check opens the issue `PE13 cloud build
<build> failed`, assigned to the owner, or comments on it if it is open. Re-run
the failed jobs, or dispatch that stage again from the fixed commit.

[`docs/evidence/`](docs/evidence/) keeps the admission records of the manually
admitted builds as history; bootstrap no longer reads them. See the
[execution record](docs/standard-runner-build-plan.md) for historical attempts
and the reason for the production kernel short-source policy, which follows the
kernel revision that the source manifest pins.

## The ZIP and its installation

A release carries `crux-pe13-<build>.zip`, its `.sha256`, `README.txt` and the
pinned RAM-only install TWRP (`config/install-twrp.json`) with its `.sha256`.
The ZIP is a crux-install/1 clean install: the update-binary checks the
signature with the TWRP's key, the device serial (`any` for these identity-free
FITs), the Recovery container U-Boot and the live partition layout before it
writes `pesystem`/`pevendor`, formats the PE data and metadata volumes and stages
both FITs for U-Boot to land. Follow the ZIP's `README.txt`: boot the install
TWRP with `fastboot boot` from the Recovery container's menu, push the ZIP and
install it in TWRP.

## Checks

```sh
python3 -m unittest discover -s tests
make -C installer test
```

For hosted diagnostics, `probe.yml` measures runner resources and
`test-artifact-retention.yml` exercises the live retry/failure contract of
`retain-artifact`. Neither produces compilation inputs.
