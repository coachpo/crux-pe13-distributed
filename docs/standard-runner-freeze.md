# PE13 hosted build and ZIP release operation guide

This is the current operation guide; [README](../README.md) is the pipeline
overview. Dated plans/acceptances retain evidence for their original inputs and
versions, not additional supported dispatch paths.

The source pins come from `config/cloud-source-manifest.json`. Historical XML,
revision and offline-transport records are retained in
[`evidence/legacy-source-inputs/`](evidence/legacy-source-inputs/README.md), never
used as an implicit packaging source selection. FIT packaging requires the
explicit source-declarative boot contract; binary reference DTBs remain only
independent equivalence fixtures. The A/B main paths are unchanged.

## Normal entry

The scheduled nightly checks for changes. To request a complete build even with
unchanged sources, manually dispatch `nightly.yml` on main:

```sh
gh workflow run nightly.yml --repo coachpo/crux-pe13-distributed --ref main
```

It allocates the build number, pins sources and marker, then starts freeze →
prepare → compile → assembly → CI signing/publication without local inputs or
per-stage manual dispatch. An active chain prevents a duplicate. Assistants need
explicit owner authorization for dispatch/publication; the configured schedule
runs independently. Installation is a separate, destructive device operation.

The stage controls below are for diagnosis/recovery, not a second normal build
procedure. Do not dispatch a stage again when its automatic successor is already
queued/running. Preserve matching build, source and execution-code identities.

W9 is under integration; hosted cache-hit/miss, retry and signed Release
acceptance are pending. To request a new chain without native output reuse:

```sh
gh workflow run nightly.yml --repo coachpo/crux-pe13-distributed --ref main \
  -f force_cold=true
```

This still allocates a new build identity and retains matching source-layer
reuse; it neither deletes caches nor modifies an accepted local output.

`freeze-standard.yml` and `prepare-standard.yml` move PE13 source acquisition,
Soong/Kati graph generation, graph indexing, sharding and cold-input auditing
to the public repository's standard `ubuntu-22.04` GitHub-hosted Linux
runners. No larger or self-hosted runner is used.

This workflow prepares build inputs. `m --skip-ninja bootimage recoveryimage
systemimage vendorimage` generates the four-image build graph; it does not compile
the ROM. `prepare-standard.yml` indexes that graph, creates and audits shards,
and records input descriptors; it does not assemble or deploy ROM/Recovery images,
publish the ZIP, or install anything on a device. Historical
[preparation](standard-runner-preparation-acceptance-2026-11-04.md) and
[compile/assembly](standard-runner-rom-build-acceptance-2026-11-05.md) acceptances
record the original standard-runner validation. Current publication is the
signed TWRP ZIP, not the retired phone OTA channel.

Both workflows can be dispatched entirely from the repository's GitHub Actions
page. The CLI commands below are optional remote controls, not local build steps.
For a new build use nightly rather than inventing a build number against an old
marker patch. A successful full freeze automatically dispatches full preparation;
preparation automatically slices, audits and continues the chain. Accept it only
after every planned input audit and the aggregate registry succeed. Current cold
capsules are compressed later, only for tasks with a native cache miss.

## Identities and reuse

- `build_number` and `build_datetime` identify a graph. Use a new build number
  for a new build. A retry intended to reproduce the same graph must pass the
  same build number **and** the same explicit Unix `build_datetime`; if omitted,
  freeze uses that run's start time. A changed build identity needs a fresh
  `freeze-standard` graph and new index, shard plan and input audits. Do not carry
  those derived graph artifacts across build identities; native content reuse
  follows the separate W9 contract below.
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
  graph/source/index bindings, plan digests and input-audit receipts before using
  them. A new source manifest or build identity requires regenerating the
  affected downstream stages.

## W9 native-shard content reuse

The current graph, source selection, index, slice inventory and registry are
always admitted for the new execution. Global consuming-stage `pipeline_code`
checks remain: a pipeline code change requires a new full freeze rather than
continuing against another revision's prepared artifacts. Source-layer reuse is
the existing, separate byte-contract mechanism.

Each preparation audit computes a descriptor of the resolved Ninja graph and
source closure, including file content/modes/links, external inputs, tools and
required interpreters. Native resolution additionally binds the actual host image,
package inventory, consumed system files/tools, fixed native environment and
verified predecessor output content. It waits for the preceding wave's real
outputs before deriving a key; matching task names or Git commits alone are not
cache evidence. No build scalar or consumed source value is normalized away.

The execution digest covers implementation/policy, excluding the two generated
source-lock JSON documents. Their effective source contents remain hashed in
each audited descriptor. A marker-only lock update therefore does not itself
invalidate unrelated task keys; tasks that consume changed marker/build data
still miss. The kernel owner gets a per-execution nonreuse value because current
`mkcompile_h` reads date/user/hostname outside its declared inputs. Kernel source,
Image/symbol pairing and those native values are not changed to manufacture hits.

Resolution reads only eligible successful native origins and verifies the
versioned content key, artifact bytes/digest and full native archive inventory.
A hit creates a current-execution adoption receipt with current dependency/run
bindings plus the unchanged original native receipt/origin. It is not a claim
of current recompilation. Older outputs without the W9 contract, absent/expired
entries and nonmatching keys are misses, not retroactively approved caches.

Only misses mount the current source snapshot, restore the exact current
shard/index/derived graph, pack a cold capsule and recheck its full audit
descriptor before ordinary native compilation. Hits skip both capsule packing
and native compilation. Nine dependency waves remain; W10 slicing and W11 ccache
are not implemented, and `CCACHE_DISABLE=1` remains required.

`force_cold=true` disables native cache adoption for the new execution. It is
available at nightly and diagnostic freeze/compile entry points and propagates
through full preparation. It does not turn off source integrity checks or erase
anything. Explicit expired preparation/source inputs still fail closed; the
reuse miss path is not permission to select another graph, source run or build.

The approved automatic fallback is one same-pin forced-cold compile after a
typed compile/assembly/preflight **content-validation** failure that records
actual reused tasks. It preserves admitted build/source/code inputs, marks the
original compile execution and cannot retry an already cold execution again.
Transport/API errors, key/signing failures and Release publication errors do not
qualify. This is not a blanket nightly retry policy. Hosted acceptance must
exercise the integrated retry boundary before it is marked verified.

Assembly, image/FIT gates, private snapshot checks, signing and installation
contracts remain in force for built and adopted results. B's source, outputs,
incremental cache, signing path and device-accepted rollback inputs are unchanged.

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
workflow token does not cover these private repositories. Only trusted `main`
snapshot-layer steps receive the credential. The secret is
not sent to redirected asset hosts or native Git/build subprocesses. This
protects the private source origin and credential; the resulting source
layers and downstream artifacts in the public build repository are public.

The snapshot layers (currently `source-15` and `source-26`) are rebuilt from
their locked assets; `reuse_sources_from` only offers unchanged, verified
non-snapshot layers. Since the global manifest changes with the new
providers, generate a fresh graph, index, shard plan, and input audits. The
complete-run `source_run` override remains exact-manifest only.

When replacing one of these snapshots, review its `snapshot.json`, private
repository and uploaded Release asset, and independently verify the tarball
bytes, embedded commit and extracted Git tree. Update the commit, tree and
release/asset lock consistently in `config/cloud-source-manifest.json` and
`config/cloud-source-overrides.json`, using nightly's validated snapshot update
route for routine builds. Manual lock changes must pass source-configuration
tests and all three source checks. Do not regenerate the entire manifest without
the matching complete layout inputs or substitute a public archive. Rotate the
credential under the same Actions secret name
with the same two-repository read scope; neither operation needs Python code
changes.

Maintained Crux forks are selected in `config/cloud-source-overrides.json`. From build
`2026110804` the device tree is fetched at fork commit `978a10a` (the development
product only), `bootable/recovery` at `44443e3`, the kernel at `086b838`, and
`packages/apps/Updates`, which is not built, at upstream commit `50c6109`.
The Updates transport now points directly to
`PixelExperience/packages_apps_Updates` at the same full commit
`50c6109df4a3af5ecba94a1382a2d5d2460422fa` (Git tree
`d19e8d96acbf4d332acfbf9052e1f2c593779771`). The Updates project remains in
the source graph; only its transport changes from the retired Crux fork to
the official repository at the identical commit and tree. Rebuilding a retained
build whose source lock names the old fork requires this explicit URL migration;
the loader does not silently redirect historical source locks.
Each build adds its own image marker,
`config/source-patches/device-crux-dev-marker-<build>.patch`, and the patched
device tree is checked against the pinned `expected_tree`. `nightly.yml`
(`scripts/nightly.py`) moves these pins to the explicit Android 13 maintenance
branches in its `TRACKED` table and to the commits the snapshot repositories
record, and writes each CI
build's marker patch and `expected_tree` the same way the freeze checks them; a
manual pin change must keep `tests.test_cloud_source_config` passing. A local
build of the same commits is the same code; `scripts/check_local_source.py`
checks a local tree against these pins. The pinned snapshot omits Git
object databases, old build outputs and local diagnostics; repo copy/link
entries are explicit manifest inputs.

Manual dispatch of `nightly.yml` forces this full chain even without a source
change, allocating its build number and marker automatically. An active chain
still prevents a duplicate. Scheduled checks retain change-only behavior. Failures
are reported; the only automatic cold retry is the typed, once-only W9 exception
described above, not a general retry of every failed chain.

Consuming stages require the upstream execution commit to match their own,
including partial preparation and offline-signature release operations. Retry
failed jobs on the same revision; after a pipeline code fix start a new full
freeze instead of mixing old stage artifacts with new code. Reusable source
layers still follow their existing content/contract verification.

## 2. Index, shard and audit cold inputs

All stages run on `ubuntu-22.04`. `prepare-standard.yml` accepts `index`,
`shard`, `pack` or `full`:

- `full` creates a fresh canonical index, slices and audits the graph, then
  starts one parallel cold-input closure audit for every shard.
- `index` creates only the canonical index. Use its run ID as `INDEX_RUN` for
  a later `shard` stage.
- `shard` uses `INDEX_RUN` to create the runtime dependencies, cold probes,
  shard plan and pruned index. Use its run ID as `SHARD_RUN` for a later `pack`
  stage.
- `pack` is the preparation-stage name for auditing every shard from `SHARD_RUN`
  and producing the deferred registry, not compressing archives. Supply the
  matching `INDEX_RUN` as an additional consistency check.

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
skipping receipt or content checks. This matters for the parallel input audits:
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
5. The parallel input-audit stage has exactly one successful receipt for every shard
   in the plan. All receipts must bind to the same build, graph, source
   manifest, index and plan; they must report a matching pruned-index identity
   and versioned semantic input descriptor. The aggregate `pack-registry` must
   report deferred packing, success and the expected shard count. Actual capsule
   archive size/SHA and content verification occur later for each miss.

Record the workflow commit, build number and datetime, manifest digest, source,
graph, index, shard and pack run IDs, and the relevant receipts/logs. An unverified
required gate leaves acceptance pending; an intentionally skipped stage with a
compatible accepted upstream run is a supported resume, not a missing gate.
Original build/run bindings remain in the dated preparation and ROM acceptance
records. Select current inputs from the successful runs of the chain being
operated, not from a historical table.

Artifacts are temporary Actions records. W6 applies the following retention to
new uploads only; existing artifact expiration times are not shortened:

| Role | Retention |
| --- | --- |
| `cloud-source-<nn>`, graph, index, shard plan, input-audit receipts/registry | 3 days |
| `shard-decision-<task>`, missed-task `cloud-runtime-capsule-<task>` | 3 days |
| `cloud-fresh-rom-<compile_run>` and `zip-manifest` | 3 days |
| `failure-source-<nn>` | 7 days |
| Native/adopted `shard-<task>`, `fresh-receipt-<task>`, `cloud-build-context`, `cloud-fresh-build-summary` | 30 days |
| Artifact-upload maintenance fixtures | 1 day |

The repository default artifact/log policy remains 90 days; Releases are not
cleaned. Retention starts at each upload, not chain completion. Every required
artifact must still exist when consumed, so the earliest required expiry limits
stage retries and signing/publication. Normal CI signing needs `zip-manifest`;
release verification also reads the assembly artifact and compile context/summary.
Keeping the small compile records does not extend the three-day assembly window.
Expired explicit inputs fail closed. Existing source-layer reuse may fetch fresh
pinned sources when unavailable, but this is not W9 compiled-output reuse or a
general automatic cold-build fallback. Re-derive missing stages, or rebuild from
the same pins with matching execution code/inputs, without bypassing validation.

Keep compact receipts/logs needed for validation, not permanent local
archives of Actions ZIPs, source layers or compiled inputs. The retained output
is the signed Release; reconstruct intermediate inputs from their pins when
needed. Existing specifically retained artifact exceptions belong to the
workspace component record, not a general archiving rule.
These artifacts can be regenerated from the pinned source manifest, workflow
code and run inputs, subject to continued availability of their pinned public
source revisions and required archive sources.

### Official Make/build-tools and host-profile boundary

The production manifest/override fix `prebuilts/build-tools` at official
`7d311e630d48dcd46e6036e34e4d588bd9c064c9`, tree
`cc04d5c641361efacb1a746ec2642abd17b4a9f9`. It is not a moving fork input.
Make is fixed at official `0189aa1e52e4461d6bfd9976f6d602e23738b34c`, tree
`5e0dd4eaced959b83860c62ff57a269c828f7057`, also outside fork tracking.
A receives the normal nsjail executable for Soong's supported runtime probe.
`graph-host-sandbox.json` and the graph receipt record actual Soong command-log
observations: wrapped commands, explicit fallback, mixed, or unobserved. A
binary's presence alone is not evidence of selection. This observation reads
logs without changing the probe or forcing success, and does not describe the
separate direct-Ninja workers as sandboxed.

B's own private source overlay hides nsjail and disables dexpreopt/profile for
Rosetta; shared source stays official and clean. A2 enables the official defaults
in A. `graph-dexpreopt.json` and the receipt report the actual generated
`soong/dexpreopt.config`; normal freeze uses `--require-dexpreopt` and refuses
disabled, boot-only, missing or invalid configurations after successful graph
generation. The observation never changes those flags or suppresses build errors.
Module-level `DisablePreoptModules` exclusions are recorded separately as supplied
by upstream/product configuration; the global gate does not rewrite or prohibit them.
Graph configuration and host build success do not establish device boot/performance.

The Android target-files re-signing interface and its former local fix are out
of maintained Crux scope; do not use it as a ZIP signing alternative. The supported
format/key path remains crux-install/1 with `crux_zip.py`. Legacy transport profiles and dated source
records are not current production manifests and retain their original pins
for historical rebuild contracts; existing forks are not deleted.

## 3. Admit preparation for native compilation/reuse

A successful full freeze dispatches its full preparation, and a successful
`full` or `pack` preparation dispatches `build-standard.yml` with its own run as
`pack_run`, its shard run and the same build number. Bootstrap admits the
preparation from run artifacts alone: the pack registry and slice plan of the
preparation, the graph receipt of the freeze run that registry names and the
canonical index receipt of its index run. The graph receipt must be the
successful graph of this build and source manifest, and the index receipt must
name that graph run, build and manifest and the SHA-256 of that exact graph
receipt. Every other check stays: registry and shard receipt bindings, plan
digest, nine dependency waves, shard manifest digests, unique output owners,
frontend identity, product namespace and kernel exports. The shard count follows
the graph (40 for the official-Make, dexpreopt-enabled A2 graph). Earlier
eight-wave graphs use their matching pipeline revision when rebuilt. If source
or graph shape changes beyond a fixed policy (runtime/kernel policy, slice and
producer requirements, image/FIT expectations), the chain stops at that check
and reports it. Retry failed jobs on the same revision; after a code/policy change
start a new full freeze rather than mixing stage revisions. Do not bypass
the admission checks. The records under `docs/evidence/` are the admissions of
earlier, manually admitted builds.

## 4. Compile, assemble and publish

Compile automatically dispatches assembly; assembly automatically dispatches
`release.yml` with `operation=release`. The release verifies lineage, signs the
assembly's install manifest in the main-only `install-signing` environment,
checks its public-key signature and payload digests, then publishes the ZIP,
checksum, README and pinned install TWRP/checksum. No offline signature or draft
approval is required by the normal chain. Releasing does not install anything.

Release verification reads the successful compile's context and summary, binds
every task to that execution/source/plan and validates built/adopted native
origins, then derives the exact assembly owners
from the required image, identity and paired-kernel exports. The delivery must
name exactly those complete producer bindings; their count is not fixed.

Only if an automatic handoff did not start, or for a reviewed stage retry, the
equivalent advanced controls are:

```sh
gh workflow run build-standard.yml --repo coachpo/crux-pe13-distributed --ref main \
  -f pack_run=PACK_RUN -f shard_run=SHARD_RUN -f build_number=BUILD_NUMBER
gh workflow run assemble-standard.yml --repo coachpo/crux-pe13-distributed --ref main \
  -f build_run=BUILD_RUN
gh workflow run release.yml --repo coachpo/crux-pe13-distributed --ref main \
  -f assembly_run=ASSEMBLY_RUN -f operation=preflight
```

These are alternatives at separate stage boundaries, not a sequence to launch
at once. Each upstream run must be successful and use the current execution
commit. `preflight` verifies without Release writes; `release` stages and publishes
in one run. `draft`/`publish` and a public offline signature remain supported for
controlled recovery. Do not pass local ROM output or bypass source/receipt gates.
For a reviewed forced-cold compile from still-valid preparation, add
`-f force_cold=true` to the compile dispatch. Do not manually set `cold_retry_of`;
that provenance field belongs to the once-only automatic content-failure path.

Inspect the published Release's source provenance and required assets. Hosted
acceptance is not device acceptance, UI/function testing or a rollback change.
