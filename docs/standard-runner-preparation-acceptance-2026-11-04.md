# Standard runner PE13 preparation — acceptance handoff

**Status: accepted for standard-runner build preparation.** Source acquisition,
fresh graph generation, canonical indexing, runtime dependency completion and
pruning, sharding, and parallel cold-input capsule packing passed on public
standard GitHub Actions runners for build `2026110303`.

This work replaces local PE13 build preparation. It generates the four-image
Soong/Kati graph, indexes it, derives the shard plan and packages cold-input
capsules. The graph command uses `m --skip-ninja`; it does **not** compile the
ROM. This acceptance does not cover ROM/Recovery assembly, OTA publication, or
device installation. The earlier accepted `dev-pe13-2026101105-v3` delivery is
unchanged and separate.

## Accepted gates

The source manifest is
`config/cloud-source-manifest.json`, SHA-256
`2950cc77ceea78dd47d140e611ef38e477823f306d841433b72f167e0ff1c69d`. It pins
1,170 projects in 32 layers and 51 copy/link layout entries.

| Gate | Actions evidence | Accepted result |
| --- | --- | --- |
| Source and fresh graph | Full freeze run `41677764699`, commit `154c1dc5ea9ba8ed0fed7c108798d3bc0695afc9`; artifact `cloud-graph-2026110303`, ID `13254050598` (800,535,901 bytes), plus 32 `cloud-source-00` through `cloud-source-31` artifacts | All 32 source layers and graph job succeeded. Graph identity: build `2026110303`, epoch `1793724452`, declared fingerprint `Xiaomi/crux/crux:13/TQ3A.230901.001.B1/2026110303:userdebug/test-keys`. Mounted source images totaled 31,927,148,544 bytes. Fresh four-image graph generation completed in 7:28.79; maximum RSS 14,700,164 KiB. Resource samples recorded peak swap use 5,517,811,712 bytes and minimum available disk 61,768,339,456 bytes. |
| Canonical index | Index run `41685367061`, commit `9c89b4740e3f6e6dc5348387787b9a9bed1a89d4`; artifact `cloud-index-2026110303`, ID `13255112960` | Successful fresh index from graph `41677764699` and the same manifest; all four graph digests validated. The index has 1,749,436 edges, 1,961,774 outputs and 2,441,929 statements across four graph files. All four target producers are present: `bootimage`, `recoveryimage`, `systemimage`, `vendorimage`. SQLite size 8,154,189,824 bytes; indexing took 5:42.37, maximum RSS 285,180 KiB. |
| Runtime completion, pruning and slicing | Run `41769467686`, commit `38028815a501c96e24857bbbe783f3f5655b927d`, successful shard job `126375516290`; artifact `cloud-shards-2026110303`, ID `13296156332` (610,880,810 bytes) | 136,591 retained edges / 136,566 actions; 39 shards / 8 waves. Pruned index 1,534,316,544 bytes. Full source/pruned closure equivalence, scalar rematerialization and the 7-action cold Go probe passed. Native preparation took 34:39.00, maximum RSS 12,741,012 KiB. |
| Parallel capsule packing | Pack-only run `41779197896`, commit `9b4e3e24d360685a6e0eecfdd91ad206de3531ce`; summary job `126422582930` | Aggregate registry artifact `cloud-pack-registry-2026110303`, ID `13300960503`, 11,210 bytes, GitHub digest `sha256:38024650392f1703a18ac25fde6c3e250b2e74dc0c3ac8fbec62a4bbbd853387`. Status `success`, all 39 capsules verified; archive total 40,834,624,242 bytes, input total 141,030,747,546 bytes. Plan SHA-256 `0adc8fc5ee03139e5918ecb6ea33cbc2048f955721cafe5567f75ee15f28df71`; common pruned index 1,534,316,544 bytes, SHA-256 `52b19246e25a2ca94b74b9d0b70a7553eca3798d9aca91f4561733b91bdf94eb`. Maximum native pack-process RSS was 2,276,388 KiB; longest native pack invocation took 1,081.518 s, excluding runner setup and transport. |

Compact evidence is retained in the repository: [source artifact identities](evidence/standard-preparation-2026110303/source-artifacts.json),
[graph receipt summary](evidence/standard-preparation-2026110303/graph-receipt-summary.json),
[canonical index receipt](evidence/standard-preparation-2026110303/index-receipt.json)
and [resource summary](evidence/standard-preparation-2026110303/resource-summary.json).
The graph summary omits the duplicate project list, retains the source-manifest
digest, and records the original full receipt's digest; the index receipt binds
to that same original digest. Large graph/index payloads are not committed.
The [graph run](https://github.com/coachpo/crux-pe13-distributed/actions/runs/41677764699)
and [index run](https://github.com/coachpo/crux-pe13-distributed/actions/runs/41685367061)
provide the hosted logs and artifacts; raw copies are also retained in the
workspace evidence directory below.

Pack acceptance is summarized by the compact [runner acceptance record](evidence/standard-preparation-2026110303/runner-acceptance.json),
[pack registry](evidence/standard-preparation-2026110303/pack-registry.json),
[condensed pack receipts](evidence/standard-preparation-2026110303/pack-receipts.json)
and [capsule artifact inventory](evidence/standard-preparation-2026110303/capsule-artifacts.json).
The condensed receipts summarize shared run bindings and per-capsule results;
the original 39 workflow receipts remain in the workspace evidence directory.

## Parallel pack gate — accepted

Earlier hosted attempts are diagnostic evidence, not accepted slice results.
Run `41765283807` (commit `aedf0e14ed9fd568305a3d50eba9283a134302e4`) passed the
real cold Go frontend probe (7 actions; `expr=3`; `tr=CRUX`; `bpglob --help`
exit 0), the native scalar probe and dependency fixed point (136,591 edges,
then +0). It failed prune validation because producer metadata referenced the
DTBO producer outside the selected closure. Therefore that run did not establish
successful slicing or capsule packing.

The prune validation fix and streaming artifact/API transport passed the shard
job in run `41769467686`. Its pack-plan also validated all 39 shards, but its
pack job was skipped by the implicit status condition inherited from an
intentionally skipped index ancestor; summary correctly failed on missing pack
receipts. The complete run is therefore not a successful pack result.

The pack-only run `41779197896`, commit
`9b4e3e24d360685a6e0eecfdd91ad206de3531ce`, reused accepted shard run
`41769467686`, graph run `41677764699` and index run `41685367061`. It completed
successfully with 43 total jobs: 41 succeeded (39 capsule jobs, `pack-plan` and
`pack-summary`) and two were intentionally skipped (`index` and `shard`) because
this dispatch ran only the pack stage. Those skips are expected for this resume
and do not omit any required preparation gate.

The aggregate registry reports `status=success`, 39 capsules, total archive
bytes 40,834,624,242 and total input bytes 141,030,747,546. All 39 raw workflow
receipts report successful packing and bind to build `2026110303`, graph run
`41677764699`, index run `41685367061`, shard run `41769467686`, source run
`41677764699`, manifest digest and plan digest. The summary also verifies a
common pruned-index identity and each capsule's archive size and SHA-256. This
completes preparation acceptance for build `2026110303`. The uploaded pack run
contains exactly 79 artifacts: 39 capsule artifacts, 39 corresponding receipts
and the aggregate registry; the receipt inventory matches the plan, and each
verified file count matches its capsule output manifest.

| Required evidence | Current record |
| --- | --- |
| Pack run and commit | `41779197896` / `9b4e3e24d360685a6e0eecfdd91ad206de3531ce`; conclusion `success`; 43 jobs = 41 success + 2 intentional skips |
| Shard job conclusion and receipt | **Accepted** — [shard receipt](evidence/standard-preparation-2026110303/shard-receipt.json), [prune record](evidence/standard-preparation-2026110303/prune-record.json), [slice inventory](evidence/standard-preparation-2026110303/slice-inventory.json), [indexed graph inventory](evidence/standard-preparation-2026110303/graph-inventory.json), [resources](evidence/standard-preparation-2026110303/shard-resource-summary.json) |
| Pack matrix | **Accepted** — 39/39 unique expected shard receipts successful, with common source/graph/index/build/plan bindings and pruned-index identity |
| Aggregate registry | **Accepted** — `cloud-pack-registry-2026110303`, artifact ID `13300960503`, digest `sha256:38024650392f1703a18ac25fde6c3e250b2e74dc0c3ac8fbec62a4bbbd853387`, status `success` |
| Overall preparation acceptance | **Accepted** for the source/graph/index/shard/pack preparation chain on public standard Linux runners |

The repository suite recorded 403 tests with 16 skips; actionlint passed for
the workflow changes. These fixture and static checks complement the hosted
acceptance; they do not imply a ROM build. No PE ROM compilation, assembly, OTA
release or device operation is claimed.

## Use and evidence retention

Use the [standard runner usage guide](standard-runner-freeze.md) for exact
dispatch commands, stage sequencing, source/run compatibility and retry rules.
The accepted lineage is graph `41677764699` → index `41685367061` → shard
`41769467686` → pack `41779197896`, all for build `2026110303` and manifest
SHA-256 `2950cc77ceea78dd47d140e611ef38e477823f306d841433b72f167e0ff1c69d`.
Follow the usage guide's identity rules when repeating these stages or starting
a new build; do not reuse derived artifacts across build identities.

The workspace evidence directory is
`out/crux-pe13-standard-freeze-2026-11-03/`:

- `accepted-graph-41677764699/` contains run/job metadata, graph logs, source
  mount receipts and resource samples. Its Actions graph artifact is
  `cloud-graph-2026110303` (ID `13254050598`).
- `accepted-index-41685367061/` contains run/job metadata and the index log. Its
  Actions index artifact is `cloud-index-2026110303` (ID `13255112960`).
- `failed-shards-41765283807/` contains the failed shard receipt and logs; retain
  it to show why that run is not acceptance evidence.
- `pack-41779197896/` contains the successful pack run metadata, all 39 raw
  workflow receipts, artifact inventory and registry. The compact repository
  records [runner/job acceptance](evidence/standard-preparation-2026110303/runner-acceptance.json),
  [registry](evidence/standard-preparation-2026110303/pack-registry.json),
  [condensed receipt summaries](evidence/standard-preparation-2026110303/pack-receipts.json)
  and [capsule artifact IDs](evidence/standard-preparation-2026110303/capsule-artifacts.json).
- The directory also holds local check logs, including
  `checks-prune-profile.log`.

The [accepted shard run](https://github.com/coachpo/crux-pe13-distributed/actions/runs/41769467686)
and [accepted pack run](https://github.com/coachpo/crux-pe13-distributed/actions/runs/41779197896)
provide hosted evidence. The [failed prune run](https://github.com/coachpo/crux-pe13-distributed/actions/runs/41765283807)
is retained as diagnostic evidence, not a passing shard result.

Actions retains source-layer artifacts for 7 days and graph, index, shard,
per-capsule, pack-receipt and pack-registry artifacts for 14 days. Download
required receipts and logs before expiry. Keep a compact, repository-retainable
acceptance record with run/commit/artifact IDs and the small JSON receipts,
source/graph/index identities, slice inventory/plan receipt, per-shard pack
receipts and aggregate registry. Large graph/index/capsule payloads remain
Actions artifacts and are not committed. Downloading them to the local machine
is not required to run this chain; local graph/shard copies and raw logs are
inspection archives only. Future cloud regeneration relies on continued
availability of the pinned public source revisions and archive endpoints.

All actual PE graph/index/slice/capsule preparation ran on public standard
`ubuntu-22.04` runners. Local work was limited to code edits, source/layout
metadata and evidence inspection, and fixture/unit checks. No actual PE graph,
index, slice or source capsule was generated locally; no local VM tree, `OUT_DIR`,
host binaries or other local build outputs supplied build inputs. The historical
`/home/qingli/.../pe13` source path is reconstructed on each runner from public
pinned source layers; it is not a connection to the original local VM. The
public pinned source/archive endpoints must remain available for future rebuilds.
