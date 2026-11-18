# OrbStack isolated cold replay

This is a **local diagnostic**, not an Actions runner or a publication input.
It calls the existing `scripts/worker.py`; it does not implement another
compiler, cache-adoption path, dependency-export policy or signing interface.
Do not register it as a self-hosted runner.

## Execution boundary

Each task gets a new `linux/amd64` Ubuntu22.04 container in the explicit
`orbstack` Docker context. SOURCE and OUT must not exist before restoration.
Native execution has no network, credentials, host HOME, full PE source mount,
B source/OUT/cache mount, or previous task OUT. Only that task's verified cold
capsule, its complete declared predecessor archives and necessary pipeline
code/configuration are read-only mounts. Implementation bytes are snapshotted
before launch, so editing the checkout cannot change an in-flight replay. The
snapshot and its local-runner digests are retained with the result. A new diagnostic output directory is
the only writable host mount. Container SOURCE/OUT disappear at exit; logs,
verified output archives and explicitly local receipts stay in that directory.

The kernel task uses the existing validated `/k` bind-mount helper, not a
symlink or B-specific source patch. Only that task receives `SYS_ADMIN` for the
container-local bind mount. Every native job retains `CCACHE_DISABLE=1`.

The runtime records its immutable Docker image ID, architecture, kernel,
package/tool/runtime identity and reference inputs. AMD64 execution on an ARM64
OrbStack host is an emulated diagnostic, **not native GitHub-runner parity**.
Do not apply Rosetta/B-only compatibility patches and label the result A-equal.
The image supplies Jammy's headless Java 11 for R8 launchers that resolve
`java` from the worker's OS PATH; it does not replace the graph's pinned JDK.

## Frozen Actions input replay

Obtain the selected successful run's `cloud-build-context`, its task-specific
`cloud-runtime-capsule-<id>` and exactly the `shard-<predecessor>` artifacts named
by `context.tasks[id].depends_on`. Standard `gh run download` or the existing
artifact reader can do this outside the native container. Keep their original
receipts intact. The tool checks the packing audit, manifest, source selection,
capsule size/SHA, complete archive contents and every producer's provenance and
output archive. Missing/expired inputs are errors, not fallback permission.

```sh
python3 local_replay/replay.py image
python3 local_replay/replay.py prepare \
  --context /absolute/inputs/context.json --id w001-s004 \
  --capsule-directory /absolute/inputs/cloud-runtime-capsule-w001-s004 \
  --dependency-root /absolute/inputs \
  --output /absolute/diagnostics/eigen-input.json
python3 local_replay/replay.py run \
  --input /absolute/diagnostics/eigen-input.json \
  --output /absolute/diagnostics/eigen-result
```

Paths in the example are placeholders; the input record is created with
exclusive creation and results require a nonexistent directory. Replaying
another task always starts another container. `--dependency-root` enumerates
only the selected task's exact predecessor IDs, not every archive in that
directory. To consume a predecessor replayed locally, use
`--dependency <id> local <result>/native` as an explicit per-ID override. The worker still verifies
the full archive, member list, modes, hashes, links, conflicts, forbidden
consumer outputs and existing export/runtime contract. No filtered imports or
extra generated-input exemptions are introduced.

## Candidate preparation before cloud validation

See the [1653 changed-slice acceptance](candidate-acceptance-2026-11-18.md) for
the executed scope and the local ART/dexpreopt limitation.

Packing is a separate phase from native execution. Use `candidate.py prepare`
with these already downloaded, verified inputs in one directory:

| Path under `--inputs` | Input |
| --- | --- |
| `source-manifest.json` | Exact source lock of the freeze build, not the checkout's current lock unless it matches |
| `graph/` | Successful freeze's extracted `cloud-graph-<build>` artifact, including `graph-out.tar.zst` and `source/` receipts |
| `index/` | Extracted `cloud-index-<build>` artifact for the same freeze, including `index.sqlite.zst` |
| `source/cloud-source-*/` | Every extracted layer in the source run named by `graph/source/source-mount.json` |

Use the successful freeze and the canonical index built from it. An older pruned
index, a failed freeze graph, or a source layer from another lock is not a
substitute. Download outside the preparation container; the entry does not
take GitHub credentials. The current parser must match the canonical index's
parser receipt. A parser change requires a fresh canonical index for the same
verified graph, not editing the existing receipt.

```sh
python3 local_replay/replay.py image
docker --context orbstack build --platform linux/amd64 \
  --file local_replay/candidate.Dockerfile --tag crux-local-candidate:jammy local_replay
python3 local_replay/candidate.py prepare \
  --inputs /absolute/verified-inputs --build-number 2026111653 \
  --output /absolute/new-candidate
```

Preparation runs networkless in a new AMD64 OrbStack container, with downloaded
inputs and a frozen code snapshot mounted read-only. It verifies the layer
receipts and parts, graph and canonical index; then uses the current sharder and
the existing audit and pack APIs. It writes `context.json` only after packing
passes against the independent audits. `preparation.json` lists the selected
capsules in dependency order: all required assembly producers and their
transitive predecessors, plus any extra task IDs requested with `--task` and
their predecessors. The full context and separate audits cover every new slice;
`capsules/<id>/` holds the inputs to `replay.py prepare`. On a suitable machine,
`--jobs <n>` runs independent audits and capsule packing in parallel.

The full SOURCE and graph OUT exist **only in the preparation container**.
Neither B's SOURCE/OUT/cache nor the preparation container's full tree is used
by native replay. For a changed cut, run packed tasks in dependency order with
`replay.py prepare` / `run`, passing each declared predecessor as
`--dependency <id> local <result>/native`. Use the generated context, capsules
and source manifest from the same preparation; the snapshot under
`implementation/` has the matching execution bytes if the checkout changed.

Locally prepared inputs use a distinct context, not invented GitHub run IDs:

- `schema_version: 1`, `diagnostic_only: true`;
- `local_preparation: {schema: "crux-local-replay/1", tool_commit: <reference/base code commit>}`;
- the actual SOURCE/OUT paths, build number, source-manifest and plan SHA;
- `source_selection` with the exact pinned projects/revisions;
- `schedule_policy`, actual `waves` and task records from current preparation;
- each task's manifest SHA, audited `reuse_descriptor`/`input_identity`, complete
  `depends_on`, category, `extra_exports`, `local_runtime_targets` and
  `runtime_inputs` as applicable;
- `reuse_policy.execution_digest` of the current production implementation and
  the existing `kernel_source_alias` / `assembly_exports` for a kernel replay.

The execution digest binds current implementation bytes, including uncommitted
candidate changes; the reference/base commit is not a claim that those bytes
were committed or pushed.

Such a context must not contain `execution_id`, `worker_commit`, repository or
stage run IDs. Its raw packing receipt comes from `cloud_pack.pack_shard`; the
embedded bundle is re-derived against the local audit inside the container.
Use LOCAL predecessors from this same context. An unchanged historical Actions
predecessor additionally requires `--actions-context <genuine-origin-context>`;
its source selection, manifest, roots and export/runtime contract must match
exactly, and its real production receipt is validated without modifying it.
Changed cuts/pins do not qualify: replay the needed predecessor chain instead.

Run graph/contract and targeted tests first, then actual cold high-risk tasks
and the entire boot/Recovery/system/vendor packaging chain. Expand to the full
dependency chain when those boundaries change. Only after this local preflight
passes should a new engineering-change cloud acceptance be started. This is
not a runtime dependency of unattended nightly: A remains independent of the
Mac, OrbStack and local outputs. GitHub's native runner,
artifact service, permissions, install-key signing and Release still require
separate authorized hosted validation.

Every result is `diagnostic_only: true`, has local provenance and lacks GitHub
execution/fresh-build/reuse identities. Production admission explicitly rejects
diagnostic receipts. No local output may be uploaded as an A build input or
published; there is no production-key or Release command in this entry.

## Complete unsigned packaging check

After the cold boot, Recovery/vendor and system tasks pass, `prepare-assembly`
selects the exact producer set from the existing assembly owner map. Supply the
same source-lock JSON used for their context and explicit LOCAL image-producer
overrides. Keep the kernel Image/DT/symbol producer and derived inputs paired:
a separately replayed kernel is not interchangeable with a different producer's
kernel-copy/overlay artifacts merely because the source commit matches.

```sh
python3 local_replay/replay.py prepare-assembly \
  --context /absolute/inputs/context.json \
  --source-manifest /absolute/inputs/cloud-source-manifest.json \
  --dependency-root /absolute/inputs \
  --dependency w005-s002 local /absolute/diagnostics/boot-result/native \
  --dependency w006-s001 local /absolute/diagnostics/recovery-result/native \
  --dependency w007-s000 local /absolute/diagnostics/system-result/native \
  --output /absolute/diagnostics/assembly-input.json
python3 local_replay/replay.py run \
  --input /absolute/diagnostics/assembly-input.json \
  --output /absolute/diagnostics/assembly-result
```

The shown IDs belong to the measured W10 plan; select actual owners for another
plan. This new container restores only full declared OUT producer archives,
without a PE SOURCE mount. It calls existing identity/property/AVB/ramdisk gates,
the paired-source FIT recipe, the installer tests and static cross-build, and
`crux_zip.prepare`. Installer build state and its existing disposable test
fixtures live only in the container. The adapter does not call the delivery
`sign`/`finalize` interfaces. `unsigned-payload/` is the existing pre-signing
staging object, **not an unsigned update format or an installable ZIP**; the
result is marked `signed: false`, `installable: false`, and cannot be used as a
production admission receipt or uploaded as an A input.

Targeted checks: `python3 -m unittest tests.test_local_candidate tests.test_local_replay`.
