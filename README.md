# Distributed Crux PixelExperience 13 development build

This repository owns the GitHub Actions pipeline for the **development** Crux
PE13 product (`aosp_crux-userdebug`): four development images and two U-Boot
FITs. It does not build, sign or publish the stock-ABL release product. That
product's R6 Releases and feed were deleted on 2026-11-07; this repository
never read or wrote them.

## Standard-runner build baseline

The current cold inputs are for Dev build `2026110704`, the first cloud build
with the crux-install/1 update stack, from
[freeze 42412861700](https://github.com/coachpo/crux-pe13-distributed/actions/runs/42412861700)
and [full preparation 42415440751](https://github.com/coachpo/crux-pe13-distributed/actions/runs/42415440751).
Its plan has 40 shards in eight waves. It passed
[cold compile 42427170882](https://github.com/coachpo/crux-pe13-distributed/actions/runs/42427170882)
and [assembly 42444269612](https://github.com/coachpo/crux-pe13-distributed/actions/runs/42444269612),
and is published as the phone release
[`dev-pe13-2026110704-v1`](https://github.com/coachpo/crux-pe13-distributed/releases/tag/dev-pe13-2026110704-v1).
Preparation acceptance alone does not establish native compilation or image
acceptance. The previous input set, `2026110501`, passed
[cold compile 42218749916](https://github.com/coachpo/crux-pe13-distributed/actions/runs/42218749916)
and [assembly 42260118571](https://github.com/coachpo/crux-pe13-distributed/actions/runs/42260118571).
Dispatch `build-standard.yml` with its defaults while the admitted inputs are
available. The preparation and graph artifacts expire on 2026-11-21 (the
freeze source artifacts on 2026-11-14); after that, run a new freeze and
preparation, then review the new admission. `freeze-standard.yml` and `prepare-standard.yml` therefore have
no build or run defaults, and `prepare-standard.yml` defaults to `stage=full`.
After the compile's summary succeeds for every admitted task, pass that **new compile run ID**
as `build_run` to `assemble-standard.yml`. The verified
`cloud-fresh-rom-<build_run>` is an Actions artifact, not a prerelease or an
installation. The entire path uses public-repository standard `ubuntu-22.04`
runners.

| Stage | Maintained inputs and handoff |
| --- | --- |
| [Prepare](docs/standard-runner-freeze.md) | `freeze-standard.yml` uses `config/cloud-source-manifest.json` and its pinned projects, patches and toolchains to create a fresh Soong/Kati graph. `prepare-standard.yml` indexes, slices and packs it into a shard plan, a reviewed registry and one cold capsule per shard. |
| Compile | `build-standard.yml` admits the graph, shard plan, reviewed registry and runtime policy, creates `cloud-build-context` and drives eight calls to `compile-standard-wave.yml`. Shards import only declared predecessor outputs from this execution; the final summary verifies a fresh receipt for every admitted shard. |
| Assemble and verify | `assemble-standard.yml` consumes the context, `cloud-fresh-build-summary` and seven same-run producer bindings. It verifies the four images and both FITs, writes `delivery-record.json` and `SHA256SUMS`, and checks that the compressed system payload decodes to the native system image identity. |
| [Deliver to dev OTA](docs/dev-ota.md#deliver-an-accepted-standard-runner-build) | Separately dispatch `release-dev.yml` with the successful **assembly run ID** and a versioned dev tag. Preflight is read-only, draft stages the exact verified bytes, and publish explicitly makes the complete prerelease discoverable. A [phone release](docs/dev-ota.md#phone-release-signed-manifest) also takes the offline signature of its `crux-ota-manifest.txt`, which every stage regenerates and verifies. |

The [current preparation records](docs/evidence/standard-preparation-2026110704/)
pin the graph summary, index receipt and pack registry for bootstrap; they
are **build inputs**, not payload backups. The
[PE13 `2026110303` acceptance and rerun guide](docs/standard-runner-rom-build-acceptance-2026-11-05.md)
describes a separate frozen input set and its image evidence. See the
[execution record](docs/standard-runner-build-plan.md) for historical attempts
and the reason for the production kernel short-source policy.

## Dev OTA is the only supported ROM update channel

The October `2026101105` frozen path was retired on 2026-11-07. Its
build-input prereleases were deleted, and its workflows (`prepare.yml`,
`controller.yml`, `controller-wave.yml`, `wave.yml`, `assemble-dev.yml`) were
removed. The `inputs/` records and the shared scripts stay as history;
`assemble_dev.py` is still used by the standard assembly. The
[host-driven Dev OTA](docs/dev-ota.md) consumes
complete published prereleases and requires separate device-write
authorization. The [protected install entry](docs/dev-ota.md#host-client-prerequisites)
stages RAM-only TWRP v2; the original cache TWRP is a CRC witness, not the
installer. [`dev-pe13-2026110501-v1`](https://github.com/coachpo/crux-pe13-distributed/releases/tag/dev-pe13-2026110501-v1)
passed device upgrade and explicit rollback to
`dev-pe13-2026101105-v3`; keep that rollback entry. Official MIUI recovery is
the only rescue path outside Dev OTA; it is not a Dev update channel.

[`dev-pe13-2026110704-v1`](https://github.com/coachpo/crux-pe13-distributed/releases/tag/dev-pe13-2026110704-v1),
the default channel head since 2026-11-08, is the first
[phone release](docs/dev-ota.md#phone-release-signed-manifest). The phone's
own CruxUpdater installed it from Settings with data kept, starting from local
build `2026110703` ([acceptance](docs/dev-ota.md#phone-release-acceptance-2026-11-08)).

## Checks

```sh
python3 -m unittest discover -s tests
```

For hosted diagnostics, `probe.yml` measures runner resources and
`test-artifact-retention.yml` exercises the live retry/failure contract of
`retain-artifact`. Neither produces compilation inputs.
