# PE13 build preparation on standard GitHub-hosted runners

`freeze-standard.yml` and `prepare-standard.yml` move PE13 source acquisition,
Soong/Kati graph generation, graph indexing, sharding and cold-input capsule
packing to the public repository's standard `ubuntu-22.04` GitHub-hosted Linux
runners. No larger or self-hosted runner is used.

This workflow prepares build inputs. `m --skip-ninja bootimage recoveryimage
systemimage vendorimage` generates the four-image build graph; it does not compile
the ROM. `prepare-standard.yml` indexes that graph, creates and audits shards,
and packs input capsules; it does not assemble or deploy ROM/Recovery images,
publish an OTA, or install anything on a device. The accepted
`dev-pe13-2026101105-v3` delivery remains a separate existing build. The complete
preparation chain has [passed acceptance](standard-runner-preparation-acceptance-2026-11-04.md).
The separate [fresh ROM acceptance and runbook](standard-runner-rom-build-acceptance-2026-11-05.md)
continues through native compilation and verified unpublished images on the same
standard runner class; these two preparation workflows remain preparation-only.

Both workflows can be dispatched entirely from the repository's GitHub Actions
page. The CLI commands below are optional remote controls, not local build steps.
For a new build, run full freeze first; then choose Prepare with `stage=full`,
that successful freeze run's ID, and the same build number. Leave `index_run`
empty to create its new canonical index. Preparation automatically slices and
packs the complete plan. Accept the result only after every planned pack job
and the aggregate registry succeed.

## Identities and reuse

- `build_number` and `build_datetime` identify a graph. Use a new build number
  for a new build. A retry intended to reproduce the same graph must pass the
  same build number **and** the same explicit Unix `build_datetime`; if omitted,
  freeze uses that run's start time. A changed build identity needs a fresh
  `freeze-standard` graph and new index, shard plan and capsules. Do not carry
  those derived outputs across build identities.
- `config/cloud-source-manifest.json` pins project revisions, patches and
  source layout. A source revision, patch or layout change requires updating
  that manifest. Per-layer source reuse is allowed only when that layer's pinned
  project and copy/link contract is unchanged and its original receipt and every
  compressed image part verify. Changed or unavailable layers are fetched again.
- `source_run` selects a complete source run and requires all 32 layers to match
  the current manifest. `reuse_sources_from` accepts one or more comma-separated
  run IDs and tries verified layers in order; unmatched layers are fetched. A
  `source-only` run is a reusable layer checkpoint, never graph acceptance.
- Reuse graph/index/shard artifacts only for the same build identity, source
  manifest and upstream run chain. `prepare-standard` validates run receipts,
  graph/source/index bindings, plan digests and capsule receipts before using
  them. A new source manifest or build identity requires regenerating the
  affected downstream stages.

## 1. Generate a fresh graph

Dispatch a full freeze. Omit `source_run` to fetch all source layers, or pass a
complete compatible run. For a retry, use the same explicit build datetime.
Replace the uppercase placeholders with the build identity, Unix epoch and
Actions run IDs for the build being prepared.

```sh
gh workflow run freeze-standard.yml --repo coachpo/crux-pe13-distributed \
  --ref main \
  -f mode=full \
  -f build_number=BUILD_NUMBER \
  -f build_datetime=BUILD_EPOCH \
  -f gogc=50
```

To reuse verified layers from prior runs while allowing missing/changed layers
to be fetched again:

```sh
gh workflow run freeze-standard.yml --repo coachpo/crux-pe13-distributed \
  --ref main \
  -f mode=full \
  -f build_number=BUILD_NUMBER \
  -f build_datetime=BUILD_EPOCH \
  -f reuse_sources_from=SOURCE_RUN_1,SOURCE_RUN_2 \
  -f gogc=50
```

To use a complete compatible source run instead of fetching layers, add
`-f source_run=COMPLETE_SOURCE_RUN` to the full-freeze command. For a
single-layer checkpoint, dispatch only that layer:

```sh
gh workflow run freeze-standard.yml --repo coachpo/crux-pe13-distributed \
  --ref main \
  -f mode=source-only \
  -f source_layers=source-07 \
  -f build_number=BUILD_NUMBER \
  -f build_datetime=BUILD_EPOCH \
  -f gogc=50
```

The source-only run ID can later be included in `reuse_sources_from`.
`wait_for_sources=true` waits for matching active source jobs rather than
starting duplicate downloads. A successful source-only run does not produce an
accepted graph.

The successful freeze run ID is `GRAPH_RUN`. It must have a successful graph
job and the `cloud-graph-BUILD_NUMBER` artifact containing the graph receipt,
source mount receipts, four fresh Ninja files, build log and resource samples.
The graph identity and source manifest in this artifact are the inputs for the
next workflow. A source artifact alone or a fixture test is not graph evidence.

Project archives use their pinned revisions; a failed Google Gitiles archive
can fall back to fetching and archiving the same revision. The maintained
providers for `vendor/gms` and `external/faceunlock` are the two private
GitHub Release snapshots pinned in the manifest and generator overrides.
The private snapshot route has no upstream or public artifact fallback. It
checks the repository, release/tag, asset ID, size and SHA-256, then checks
the archive's Git commit and reconstructed tree before packaging the layer.
The original Archive.org bundles are upstream provenance. Only if a pinned
snapshot cannot be obtained, recovering the same commit from its original
bundle is a separate, manual last resort: verify the pinned commit and Git
tree before admitting it. An identity or integrity mismatch is an error to
investigate, never a reason to bypass verification.

Set `CRUX_SOURCE_SNAPSHOTS_TOKEN` in the consuming repository's Actions
secrets using a fine-grained token with **Contents: read** on exactly
`coachpo/crux-pe13-vendor-gms-snapshot` and
`coachpo/crux-pe13-faceunlock-snapshot`. The normal consuming-repository
workflow token does not cover these private repositories. Only the manual
trusted `main` snapshot-layer steps receive the credential. The secret is
not sent to redirected asset hosts or native Git/build subprocesses. This
protects the private source origin and credential; the resulting source
layers and downstream artifacts in the public build repository are public.

The snapshot layers (currently `source-15` and `source-26`) are rebuilt from
their locked assets; `reuse_sources_from` only offers unchanged, verified
non-snapshot layers. Since the global manifest changes with the new
providers, generate a fresh graph, index, shard plan, and capsules. The
complete-run `source_run` override remains exact-manifest only.

When replacing one of these snapshots, review its `snapshot.json`, private
repository and uploaded Release asset, and independently verify the tarball
bytes, embedded commit and extracted Git tree. Update the commit, tree and
release/asset lock together in `config/cloud-source-overrides.json` and
regenerate the source manifest from the matching resolved XML and source
layout. The generator requires these overrides and never substitutes a
public archive. Rotate the credential under the same Actions secret name
with the same two-repository read scope; neither operation needs Python code
changes.

Maintained Crux forks are selected in `config/cloud-source-overrides.json`. From build
`2026110804` the device tree is fetched at fork commit `978a10a` (the development
product only), `bootable/recovery` at `44443e3`, the kernel at `086b838`, and
`packages/apps/Updates`, which is not built, at the upstream commit `50c6109`.
Each build adds its own image marker,
`config/source-patches/device-crux-dev-marker-<build>.patch`, and the patched
device tree is checked against the pinned `expected_tree`. A local build of the
same commits is the same code; `scripts/check_local_source.py` checks a local
tree against these pins. The pinned snapshot omits Git
object databases, old build outputs and local diagnostics; repo copy/link
entries are explicit manifest inputs.

## 2. Index, shard and pack

All stages run on `ubuntu-22.04`. `prepare-standard.yml` accepts `index`,
`shard`, `pack` or `full`:

- `full` creates a fresh canonical index, slices and audits the graph, then
  starts one parallel cold-input pack job for every shard.
- `index` creates only the canonical index. Use its run ID as `INDEX_RUN` for
  a later `shard` stage.
- `shard` uses `INDEX_RUN` to create the runtime dependencies, cold probes,
  shard plan and pruned index. Use its run ID as `SHARD_RUN` for a later `pack`
  stage.
- `pack` packages every shard from `SHARD_RUN`. Supply the matching `INDEX_RUN`
  as an additional consistency check.

The four terminal producers are `bootimage`, `recoveryimage`, `systemimage`
and `vendorimage`.

One-shot preparation from an accepted graph:

```sh
gh workflow run prepare-standard.yml --repo coachpo/crux-pe13-distributed \
  --ref main \
  -f stage=full \
  -f graph_run=GRAPH_RUN \
  -f build_number=BUILD_NUMBER
```

Resumable stages, if a previous stage succeeded:

```sh
gh workflow run prepare-standard.yml --repo coachpo/crux-pe13-distributed \
  --ref main -f stage=index -f graph_run=GRAPH_RUN -f build_number=BUILD_NUMBER

gh workflow run prepare-standard.yml --repo coachpo/crux-pe13-distributed \
  --ref main -f stage=shard -f graph_run=GRAPH_RUN -f build_number=BUILD_NUMBER \
  -f index_run=INDEX_RUN

gh workflow run prepare-standard.yml --repo coachpo/crux-pe13-distributed \
  --ref main -f stage=pack -f graph_run=GRAPH_RUN -f build_number=BUILD_NUMBER \
  -f index_run=INDEX_RUN -f shard_run=SHARD_RUN
```

`graph_run` and `build_number` must identify the same successful freeze graph in
every stage. For `shard`, `index_run` must identify the canonical index created
from that graph. For `pack`, `shard_run` must identify a successful shard run
for that graph/build/source manifest; `index_run`, when supplied, must match
the index recorded by its receipt. Leave `source_run` empty to use the complete
source snapshot bound to the graph; an override must be a complete compatible
source run. Run IDs are numeric GitHub Actions run IDs, not build numbers.

Inspect the run summary and download its artifacts with, for example:

```sh
gh run view RUN_ID --repo coachpo/crux-pe13-distributed
gh run download RUN_ID --repo coachpo/crux-pe13-distributed \
  --name cloud-pack-registry-BUILD_NUMBER
```

Use `--name` to download only the artifact needed; source-layer artifacts can
be very large.

Source restoration lists a run's artifacts once and downloads by artifact ID;
ZIP bodies stream to disk in bounded chunks. Preparation artifact reads wait
until the server's reset time on a primary API rate limit, then retry without
skipping receipt or content checks. This matters for the parallel pack matrix:
GitHub documents a normal `GITHUB_TOKEN` budget of 1,000 REST requests per hour
per repository ([rate-limit documentation](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api)).
An API-budget wait is not a request for a larger runner.

Job gates use `!cancelled()` so intentionally skipped ancestors do not suppress
resumed stages, and ordinary workflow cancellation can stop work. If an older
revision's `always()` gate ignores cancellation, use
`gh run cancel RUN_ID --force --repo coachpo/crux-pe13-distributed` and confirm
`completed/cancelled` before replacing it.

## Acceptance evidence and artifact retention

Do not mark preparation accepted until the complete chain has passed:

1. The source receipts cover all 32 layers and verify each reused or fetched
   layer against its source contract and image parts.
2. The freeze run and graph job succeed with the expected build identity,
   complete mounted source snapshot, fresh external output directory and all
   four expected Ninja graph files.
3. The index run validates all four graph digests, creates a canonical index
   for this graph/build identity, and identifies the four expected image
   producers. A retry may reuse an already accepted index only for that same
   graph/build identity.
4. The shard run succeeds with its runtime dependencies and cold probes,
   audited ownership/dependency bindings, complete shard inventory and plan
   digest.
5. The parallel pack stage has exactly one successful receipt for every shard
   in the plan. All receipts must bind to the same build, graph, source
   manifest, index and plan; they must report a matching pruned-index identity
   and verified archive size and SHA-256. The aggregate `pack-registry` must
   report success and the expected shard count.

Record the workflow commit, build number and datetime, manifest digest, source,
graph, index, shard and pack run IDs, and the relevant receipts/logs. An unverified
required gate leaves acceptance pending; an intentionally skipped stage with a
compatible accepted upstream run is a supported resume, not a missing gate.
The accepted build `2026110303` uses graph/source run `41677764699`, index run
`41685367061`, and successful shard job `126375516290` in `41769467686`.
Pack run `41779197896` passed the original
[preparation acceptance](standard-runner-preparation-acceptance-2026-11-04.md),
but later native runs exposed additional SOURCE reads. The final admitted pack
is **`42001383332`**, with the same graph/index/shard identity and all 39
repacked cold capsules. See the [ROM acceptance](standard-runner-rom-build-acceptance-2026-11-05.md)
for the complete cold compile and assembly evidence.

Artifacts are temporary Actions records: `cloud-source-<nn>` and
`failure-source-<nn>` are retained for 7 days; `cloud-graph-<build>`,
`cloud-index-<build>`, `cloud-shards-<build>`, `cloud-capsule-<shard>`,
`cloud-pack-receipt-<shard>` and `cloud-pack-registry-<build>` are retained for
14 days. Download and preserve required acceptance evidence before expiry.
These artifacts can be regenerated from the pinned source manifest, workflow
code and run inputs, subject to continued availability of their pinned public
source revisions and required archive sources.

## 3. Admit preparation for a cold compile

Preparing a new graph or repacking an expired snapshot does not automatically
admit a new pack run in `build-standard.yml`. Review the resulting graph and
index receipts, source manifest, plan, capsule identities and registry. The
shard count follows the graph (39 for `2026110501`, 40 for `2026110704`); the
compile, assembly and release checks take it from the admitted registry and
keep the eight dependency waves.
For a replacement, record the reviewed registry and update its entry in
`scripts/cloud_build_plan.py` (`PACK_REGISTRIES`) and the workflow defaults.
If the graph is regenerated, also review and update the pinned graph summary
and index receipt read by bootstrap; they bind the exact graph receipt and the
independent image identity. If source or graph shape changes, review the
runtime/kernel policy, slice and producer requirements and image/FIT
expectations against the new inputs. Do not bypass the admission checks.

Once the replacement is admitted, follow the
[compile → assemble runbook](standard-runner-rom-build-acceptance-2026-11-05.md#re-run-without-a-local-build-environment).
