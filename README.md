# Distributed Crux PixelExperience 13 development build

This repository owns the GitHub Actions pipeline for the **development** Crux
PE13 product (`aosp_crux-userdebug`): four development images, two U-Boot FITs
and the signed TWRP ZIP made from them. The local OrbStack build compiles the
same pinned source and packages the same ZIP with the same tools, but does not
publish here.

## Standard-runner build baseline

The admitted cold inputs are for build `2026110801`, from
[freeze 42453128546](https://github.com/coachpo/crux-pe13-distributed/actions/runs/42453128546)
and [full preparation 42455132490](https://github.com/coachpo/crux-pe13-distributed/actions/runs/42455132490):
40 shards in eight waves. They passed
[cold compile 42465427782](https://github.com/coachpo/crux-pe13-distributed/actions/runs/42465427782)
and [assembly 42482803187](https://github.com/coachpo/crux-pe13-distributed/actions/runs/42482803187).
Preparation acceptance alone does not establish native compilation or image
acceptance. The source pins in `config/` have since moved on, so the next build
starts with a new freeze and preparation, then a reviewed admission.
`freeze-standard.yml` and `prepare-standard.yml` therefore have no build or run
defaults, and `prepare-standard.yml` defaults to `stage=full`. After the
compile's summary succeeds for every admitted task, pass that **new compile run
ID** as `build_run` to `assemble-standard.yml`. The entire path uses
public-repository standard `ubuntu-22.04` runners.

| Stage | Maintained inputs and handoff |
| --- | --- |
| [Prepare](docs/standard-runner-freeze.md) | `freeze-standard.yml` uses `config/cloud-source-manifest.json` and its pinned projects, patches and toolchains to create a fresh Soong/Kati graph. `prepare-standard.yml` indexes, slices and packs it into a shard plan, a reviewed registry and one cold capsule per shard. |
| Compile | `build-standard.yml` admits the graph, shard plan, reviewed registry and runtime policy, creates `cloud-build-context` and drives eight calls to `compile-standard-wave.yml`. Shards import only declared predecessor outputs from this execution; the final summary verifies a fresh receipt for every admitted shard. |
| Assemble and verify | `assemble-standard.yml` consumes the context, `cloud-fresh-build-summary` and seven same-run producer bindings. It verifies the four images and both identity-free FITs, writes `delivery-record.json` and `SHA256SUMS`, checks that the compressed system payload decodes to the native system image identity, then builds `installer/` and writes the unsigned ZIP payload (`zip-payload/`) with `scripts/crux_zip.py prepare`. `cloud-fresh-rom-<build_run>` is an Actions artifact, not a release. |
| Release | Sign the payload's `crux-install.txt` offline (`scripts/crux_zip.py sign`), then dispatch `release.yml` with the successful **assembly run ID**, a `crux-pe13-<build>-v<n>` tag and the signature. Preflight is read-only, draft stages the exact verified bytes, and publish makes the complete prerelease visible. |

The [current preparation records](docs/evidence/standard-preparation-2026110801/)
pin the graph summary, index receipt and pack registry for bootstrap; they
are **build inputs**, not payload backups. See the
[execution record](docs/standard-runner-build-plan.md) for historical attempts
and the reason for the production kernel short-source policy.

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
