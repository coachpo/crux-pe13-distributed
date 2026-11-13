# Distributed Crux PixelExperience 13 development build

This repository owns the GitHub Actions pipeline for the **development** Crux
PE13 product (`aosp_crux-userdebug`): four development images, two U-Boot FITs
and the signed TWRP ZIP made from them. The local OrbStack build compiles the
same pinned source and packages the same ZIP with the same tools, but does not
publish here.

## Standard-runner build chain

`nightly.yml` checks the sources every night at 23:37 UTC (01:37 in Helsinki in
winter). When the configured Android 13 maintenance branch of a tracked fork
or the commit recorded
by a snapshot repository moved, or `main` changed `scripts/`, `config/`,
`installer/`, `keys/`, `.github/` or the production
`references/pe13-boot-contracts/` since the last full freeze, it
takes the next CI build number of the Helsinki day (`YYYYMMDD50` to
`YYYYMMDD99`; local builds use `01` to `49`), pins the sources, regenerates the
device marker patch and its patched tree, commits as `github-actions[bot]` and
dispatches a full freeze. A night without changes, or with a chain run still
queued or running, starts nothing. Each stage then dispatches the next one from
its last job; partial runs (`source-only`, `index`, `shard`) do not continue the
chain. Build identities and upstream run provenance are recorded with the
[published ZIP releases](https://github.com/coachpo/crux-pe13-distributed/releases),
not maintained as a second mutable baseline in this overview.
The entire path uses public-repository standard `ubuntu-22.04` runners.

W9 native-shard reuse is being integrated; hosted acceptance is pending. The
content/admission rules below describe that implementation, not proof of a cache
hit or a newly accepted Release. W10 repartitioning and W11 ccache are not included.

Manual dispatch of `nightly.yml` forces a complete build even with no changes,
but does not start a duplicate while another chain is active. It allocates the
build number and marker just like the scheduled entry. `scripts/nightly.py`'s
`TRACKED` table names the explicit branches of all ten maintained ROM forks:
device, vendor blobs, kernel, recovery, frameworks, ART, display, system/core,
ADB and vendor/aosp. Make is fixed at official `0189aa1e52e4461d6bfd9976f6d602e23738b34c`
and build-tools at official `7d311e630d48dcd46e6036e34e4d588bd9c064c9`, with
their expected trees; neither follows the retained historical forks. Other upstream projects stay pinned;
the two private snapshot inputs keep their separate identity checks. A repository
default-branch change never selects another Android version implicitly.

Historical XML/source audit records are indexed under
[`docs/evidence/legacy-source-inputs/`](docs/evidence/legacy-source-inputs/README.md),
not used as current configuration or CLI defaults. Standalone FIT packaging
requires explicit source selection and source-declarative contract inputs;
the normal A/B callers already supply them. Retained reference DTBs are test
oracles only. Native, Recovery-only and diagnostic packaging modes remain.

The official build-tools pin supplies nsjail normally to A's Soong host probe.
Graph receipts record actual command-log sandbox observations, not a guess from
binary presence. B keeps its Rosetta nsjail workaround only in its private source
overlay. A2 restores official full dexpreopt/profile defaults in A; freeze verifies
the generated configuration rather than assuming a source pin implies it. B's
private profile keeps Rosetta preopt/profile disabled. Historical source forks
remain available for their retained build pins. Android target-files re-signing
is outside maintained Crux interfaces; delivery uses `crux_zip.py`, not that tool.

| Stage | Maintained inputs and handoff |
| --- | --- |
| Prepare | `freeze-standard.yml` uses `config/cloud-source-manifest.json` and its pinned projects, patches and toolchains to create a fresh Soong/Kati graph. `prepare-standard.yml` indexes, slices and audits the cold input closure without compressing a capsule for every shard. The registry binds the current plan and each audited input descriptor. |
| Compile | `build-standard.yml` admits the current graph/index/registry and creates `cloud-build-context`. The existing nine waves resolve each task only after its actual predecessor outputs are known. A verified hit is adopted with original native provenance and a current-execution receipt; only a miss materializes a verified current capsule and runs native compilation. The summary validates every current receipt and its original native output binding. |
| Assemble and verify | `assemble-standard.yml` consumes the context, `cloud-fresh-build-summary` and plan-owned same-run producer bindings. It verifies the four images and both identity-free FITs, writes `delivery-record.json` and `SHA256SUMS`, checks that the compressed system payload decodes to the native system image identity, then builds `installer/` and writes the unsigned ZIP payload (`zip-payload/`) with `scripts/crux_zip.py prepare`. `cloud-fresh-rom-<build_run>` is an Actions artifact, not a release; `zip-manifest` holds the payload's `crux-install.txt` for signing. |
| Release | The `sign` job runs in the `install-signing` environment: it waits for the assembly run to finish, checks its lineage and the manifest identity and signs `crux-install.txt` with the `CRUX_INSTALL_SIGNING_KEY` secret. The `release` job verifies the whole assembly artifact, builds the ZIP, checks the signature against `keys/crux-b-install-2026-11-07.pub.pem` and stages, then publishes, the prerelease `crux-pe13-<build>-v<n>`: this assembly's tag, or the next free version of its build. Manual dispatches keep `preflight` (read only), `draft` and `publish`, and accept an offline signature in place of the CI one. |

A failed stage, dispatch or nightly check opens the issue `PE13 cloud build
<build> failed`, assigned to the owner, or comments on it if it is open. Re-run
the failed jobs on the same commit. Every consuming stage checks that its
upstream runs used its execution commit before consuming inputs or signing.
If pipeline code changes, start a new full freeze (manual nightly is the unified
entry); do not continue new code against older prepared stage artifacts.
Verified source-layer reuse remains governed by its existing byte contracts.

Native reuse is content-addressed, not accepted merely because Git SHAs or task
names match. Keys bind audited source bytes/resolved graph, execution policy,
host/runtime identity and exact predecessor output content. Adoption preserves
the original native producer; it does not relabel old bytes as newly compiled.
Pre-W9 outputs without this contract are misses. `force_cold=true` on manual
nightly disables native adoption for the whole new chain without clearing any
cache or disabling independently verified source-layer reuse. Kernel owners
remain misses because the current kernel embeds date/user/hostname outside the
declared Ninja inputs; W9 does not change that native behavior.

Only a typed compile/assembly/preflight content-validation failure after actual
reuse qualifies for one same-pin cold retry. Transport, signing and publication
failures do not. Forced-cold executions cannot loop into another automatic retry.
See the [operation guide](docs/standard-runner-freeze.md#w9-native-shard-content-reuse)
for the acceptance boundary and input expiry rules.

Use the [hosted operation guide](docs/standard-runner-freeze.md) for the normal
entry, stage recovery, input reuse and release checks. Dated plans and acceptance
records describe their original runs; their old branches, defaults and commands
are not current dispatch instructions.

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

New source/graph/preparation and assembly/signing-manifest artifacts are retained
for 3 days. Source failure evidence remains 7 days, native shards/receipts and
compile context/summary 30 days, maintenance fixtures 1 day. The repository
default remains 90 days; existing artifact expiries and existing Releases are
not changed. These temporary input windows bound retries and re-publication;
see the [retention and recovery details](docs/standard-runner-freeze.md#acceptance-evidence-and-artifact-retention).
W9 adds three-day per-task decisions and missed-task runtime capsules; native
output/origin/adoption records remain in the thirty-day group. Existing objects
are not retroactively converted into native caches or given new expiry times.

```sh
python3 -m unittest discover -s tests
make -C installer test
```

`test-artifact-retention.yml` remains a maintenance test of the production
`retain-artifact` helper's live retry/failure contract; it produces no compilation
inputs. Production stages collect their own runner resource samples. The old
Release-backed wave dispatchers and one-time resource probe are retired; only
the artifact-backed chain above is maintained. Shared graph/capsule/worker and
assembly libraries remain production dependencies, with their behavior tests.
