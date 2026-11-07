# Distributed Crux PixelExperience 13 development build

This repository owns the GitHub Actions pipeline for the **development** Crux
PE13 product (`aosp_crux-userdebug`): four development images and two U-Boot
FITs. It does not build, sign or publish the public release product. The
public `coachpo/crux-pe13-ota` R6 feed is frozen and is not read or written here.

## Standard-runner build baseline

The current cold inputs are for Dev build `2026110501`, from
[freeze 42186082476](https://github.com/coachpo/crux-pe13-distributed/actions/runs/42186082476)
and [full preparation 42190106417](https://github.com/coachpo/crux-pe13-distributed/actions/runs/42190106417).
This input set passed [cold compile 42218749916](https://github.com/coachpo/crux-pe13-distributed/actions/runs/42218749916)
and [assembly 42260118571](https://github.com/coachpo/crux-pe13-distributed/actions/runs/42260118571).
New preparation results still need their own compile and assembly acceptance.
Dispatch `build-standard.yml` with its defaults while the admitted inputs are
available. After its 39-task summary succeeds, pass that **new compile run ID**
as `build_run` to `assemble-standard.yml`. The verified
`cloud-fresh-rom-<build_run>` is an Actions artifact, not a prerelease or an
installation. The entire path uses public-repository standard `ubuntu-22.04`
runners.

| Stage | Maintained inputs and handoff |
| --- | --- |
| [Prepare](docs/standard-runner-freeze.md) | `freeze-standard.yml` uses `config/cloud-source-manifest.json` and its pinned projects, patches and toolchains to create a fresh Soong/Kati graph. `prepare-standard.yml` indexes, slices and packs it into a shard plan, a reviewed registry and 39 cold capsules. |
| Compile | `build-standard.yml` admits the graph, shard plan, reviewed registry and runtime policy, creates `cloud-build-context` and drives eight calls to `compile-standard-wave.yml`. Shards import only declared predecessor outputs from this execution; the final summary verifies all 39 fresh receipts. |
| Assemble and verify | `assemble-standard.yml` consumes the context, `cloud-fresh-build-summary` and seven same-run producer bindings. It verifies the four images and both FITs, writes `delivery-record.json` and `SHA256SUMS`, and checks that the compressed system payload decodes to the native system image identity. |
| [Deliver to dev OTA](docs/dev-ota.md#deliver-an-accepted-standard-runner-build) | Separately dispatch `release-dev.yml` with the successful **assembly run ID** and a versioned dev tag. Preflight is read-only, draft stages the exact verified bytes, and publish explicitly makes the complete prerelease discoverable. |

The [current preparation records](docs/evidence/standard-preparation-2026110501/)
pin the graph summary, index receipt and pack registry for bootstrap; they
are **build inputs**, not payload backups. The
[PE13 `2026110303` acceptance and rerun guide](docs/standard-runner-rom-build-acceptance-2026-11-05.md)
describes a separate frozen input set and its image evidence. See the
[execution record](docs/standard-runner-build-plan.md) for historical attempts
and the reason for the production kernel short-source policy.

## Dev OTA is the only supported ROM update channel

The `2026101105` frozen path (`prepare.yml`, `controller.yml`,
`controller-wave.yml`, `wave.yml`, `assemble-dev.yml` and its `inputs/`
records) is retained separately from the standard-runner build. It is not a
fallback or an input to it; `assemble-dev.yml` only retains Actions artifacts.
Preparation releases are build inputs, not ROM updates, and cannot use
`dev-pe13-*` tags. The [host-driven Dev OTA](docs/dev-ota.md) consumes
complete published prereleases and requires separate device-write
authorization. The [protected install entry](docs/dev-ota.md#host-client-prerequisites)
stages RAM-only TWRP v2; the original cache TWRP is a CRC witness, not the
installer. [`dev-pe13-2026110501-v1`](https://github.com/coachpo/crux-pe13-distributed/releases/tag/dev-pe13-2026110501-v1)
passed device upgrade and explicit rollback to
`dev-pe13-2026101105-v3`; keep that rollback entry. R6 rollback and official
MIUI recovery are retained separately; neither is a Dev update channel.

## Checks

```sh
python3 -m unittest discover -s tests
```

For hosted diagnostics, `probe.yml` measures runner resources and
`test-artifact-retention.yml` exercises the live retry/failure contract of
`retain-artifact`. Neither produces compilation inputs.
