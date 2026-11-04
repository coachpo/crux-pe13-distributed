# Fresh standard-runner PE13 build

Plan: standard-runner-build / revision 5 / active (2026-11-04).

## Authority, baseline and acceptance

The owner explicitly authorized modifying/pushing workflows and triggering Actions
to compile, assemble and validate a completely new ROM from the 39 accepted cloud
capsules. Use only public-repository standard Linux runners; no local PE builds,
larger/self-hosted runners, old compiled outputs, ccache, release publication or
device writes. Actions artifacts are the delivery boundary, not GitHub Releases.

Baseline: `0ce673f85f6ebc1aff0d0ab2ee11df06d0f01de5`, clean main in
`out/crux-pe13-actions-ota-2026-10-11/actions-repository`.
Input pack run `41779197896`, shard run `41769467686`, graph run `41677764699`,
index run `41685367061`, build `2026110303`. The build identity embedded in
these capsules stays unchanged; the compilation execution is new. Source
manifest `2950cc77ceea78dd47d140e611ef38e477823f306d841433b72f167e0ff1c69d`,
plan `0adc8fc5ee03139e5918ecb6ea33cbc2048f955721cafe5567f75ee15f28df71`.
Plan waves: 13/3/7/6/5/2/2/1 tasks. Preserve accepted v3 deliveries and source/device state.

Completion requires every native shard built successfully with a fresh provenance
receipt, verified dependency imports and export archives, all four new images,
paired freshly built kernel/symbol/DTB inputs, existing image/property/AVB/cpio/FIT
gates, and an unpublished Actions delivery with checksums and acceptance evidence.
Fixtures do not prove the real build. No release, OTA/feed update or flashing.

## Fixed integration design

New artifact-backed workflows do not call the release-backed legacy controller.
An Actions bootstrap validates accepted pack registry, slice inventory and plan,
then emits compact `context.json` and matrices. Eight dependency-ordered reusable
wave calls run the existing `worker.run_shard` on clean standard Ubuntu runners.
No compilation happens on the local machine.

Context schema v1:
- `execution_id`: current compile workflow run ID; `worker_commit`: its SHA;
  `repository`, `build_number`, `pack_run`, `shard_run`, `graph_run`,
  `index_run`, `source_run`, `source_manifest_sha256`, `plan_sha256`,
  `graph_index`, `source_root`, `out_root`.
- `waves`: arrays of task IDs; `tasks`: ID-keyed records containing `id`,
  `wave`, `depends_on`, `manifest_sha256`, `archive`, `archive_bytes`,
  `archive_sha256`.
- `assembly_exports`: relevant image/ramdisk/frontend/kernel output-owner map;
  `frontend`: accepted graph receipt summary and source revision declaration.

Initial compilation is empty-state. Every dependency binding points to
`shard-<id>` in the CURRENT compile workflow run, with that run's worker SHA
and the exact admitted manifest SHA. The transport wrapper checks the producer's
additional `fresh_build` receipt field equals this context's execution and
input identities before the existing worker verifies/merges bytes. No old
reuse-state, release assets, source overlays or prebuilt OUT supplement is admitted.
If repairs require a later fresh lineage, root must revise the explicit resume
contract before using any earlier output; that permission is not implicit.

The first execution produced 15 successful fresh tasks, then its kernel failed.
A probe-only exception now permits those same-task fresh dependencies from
execution `41789247344` / worker `17f59f6` for a diagnostic kernel execution with
identical capsule/plan/manifest/source identities. This does not admit any output
from before this task or any legacy reuse-state. The probe records its parent,
is marked diagnostic-only, uploads a probe artifact rather than `shard-ID`, and
cannot satisfy the production summary/assembly guards. Default production
compilation remains empty/current-run-only; no general resume permission is added.

A worker downloads only its admitted `cloud-capsule-ID` artifact, verifies the
tar.zst byte identity against the registry, restores the cold capsule, and downloads
only admitted fresh predecessor artifacts. No full warmed graph OUT is restored.
The worker retains CCACHE_DISABLE=1 and cold output/external-input guards.
It writes `fresh_build` into receipt.json after the native worker completes:
execution_id, pack_run, shard_run, plan_sha256, source_manifest_sha256.
Output artifact `shard-ID`; separate `fresh-receipt-ID` contains receipt.json.
Summary requires all 39 unique successful receipts, validates binding/lineage,
and writes fresh producer bindings. Assembly is a separate artifact-only workflow
and cannot publish; it may use only those newly admitted producer outputs.

## Contracts and ownership

All executors read this plan and unchanged protocol below, check allowed-path
diffs before edits, preserve other writers, and return concrete evidence after at
most three in-contract fix/check cycles. Root owns integration, commits/pushes,
Actions dispatch and final acceptance; subagents never mutate remote state.

| ID | State | Owner | Allowed paths / deliverable / validation |
| --- | --- | --- | --- |
| C0 | done | root | `cloud_build_plan.py` and artifact-only workflows passed fixture/static checks and real bootstrap in run `41789247344`. Final all-task summary remains part of V1. |
| C1 | done | frontend_decomposition + root | `cloud_compile.py` validates cold roots, admitted capsule bytes, current-execution dependency receipts and manifest/worker pins, then calls native worker with cache disabled. Six fixtures and initial real native tasks passed; V1 covers the complete build. |
| A1 | done | source_footprint | `scripts/cloud_assemble.py`, `tests/test_cloud_assemble.py`. Artifact-backed fresh producer admission and independently derived identity, with graph scalar date separate from PE branding time. Six fixture tests passed; real assembly remains V2. |
| A2 | done | source_footprint + root review | Source-declarative boot contract route implemented, legacy default preserved. Policy/output equivalence and no-old-DTB-read fixtures passed; fresh assembly always selects this route. Actual FITs remain part of V2. |
| V1 | active | root + read-only observer | Current production run `41819689572`, head `f4592fed75f20c7de8ee3cd16d55516611b03d19`; all 39 native tasks and final fresh producer summary are required. It restarts empty and uses no outputs from earlier runs. |
| V2 | draft | root + acceptance_audit | New image assembly and validation, compact evidence, usage and acceptance report. Requires V1 and usable A1. |
| K1 | done | frontend_decomposition + root | Per-string overflow measured; identical source alias experiment passed complete kernel build. Validated pinned adapter promoted to production. Diagnostic artifacts are excluded from production summary and assembly. |
| D1 | active | source_footprint + root | `worker.py` and worker fixtures: optional extra exports must be a subset of the admitted manifest's declared outputs, verified fresh after Ninja, then archived with the original exports. No arbitrary OUT/source approval. Root C0/C1 derives typed launcher runtime bundles, admits their current-run owners/dependencies and requests only these extra exports. |

All actual source graph/index/slice/capsule regeneration, if a diagnosed input
closure defect requires it, also stays on standard runners and derives from the
same public source pins. Reopen dependent evidence when an input manifest changes.
Do not insert historical source overlays or old compiled artifact bindings.

## Execution record

- Initial read-only audits confirm legacy wave/controller downloads Release
  assets and has old reuse defaults; it will not be used for this task.
- Existing worker checks manifest/bundle byte identity, rejects preexisting
  outputs/imports, verifies dependency archives/specs and disables ccache.
- Existing assembler supports unpublished output but its checked-in producer/
  identity inputs are old; derive new bindings and identity for this graph.
- Local validation baseline: 403 tests, 16 environment skips from the accepted
  prep delivery. Integrated compile/assembly helpers and contract route now pass
  424 tests (16 environment skips), actionlint and whitespace checks. These are
  separate from required real compilation/assembly acceptance.
- Preflight found the old FIT recipe reads retained compiled reference DTBs and
  copies boot-contract properties. Root selected a source-declarative contract
  route, preserving the existing hardware policy and validation but removing old
  binary reference inputs from this fresh assembly. This is an input
  representation change, not a boot ABI change; no device validation is claimed.
  Legacy defaults remain unchanged. A2 must prove policy equivalence in fixtures
  and select source-contract mode in fresh assembly; no runtime old-DTB fallback.
- Real compile bootstrap succeeded in `41789247344`, publishing a new context
  with this execution ID and worker revision `17f59f6`. Wave 0 dispatched all 13
  tasks; 12 have succeeded at the current checkpoint, with no compile failure.
  No earlier compiled producer or reuse-state was admitted. Later waves and the
  separate unpublished assembly/validation workflow remain pending.
- First execution ended with 15 successful native tasks and one kernel failure:
  `w001-s001` job `126446947928` failed at Wi-Fi `wlan_hdd_assoc.o` with nested
  Make `/bin/sh: Argument list too long`. Earlier headers-install shell option
  errors are a separate anomaly. Outer Ninja argv was valid (~669 KB aggregate);
  the actual failing recipe/env was not captured, so per-string versus aggregate
  exec limit is not yet established. K1 measures it before choosing a fix.
- Diagnostic run `41797793226`, head `ad6839b`, captured the failing shell
  recipe at 135,239 bytes. Runner page size is 4,096, hence Linux's per-string
  limit is 131,072 bytes; aggregate ARG_MAX is 4,194,304 and outer environment
  is only 5,078 bytes. Source prefix repeats 740 times. This establishes a
  per-string failure, not memory pressure. The next probe may bind the identical
  admitted source tree at `/k` and pass command-line `srctree=/k` via MAKEFLAGS;
  do not set KBUILD_SRC, which skips the kernel O= bootstrap. No source bytes,
  compiler flags other than source path spelling, output ownership or old-data
  admission changes. This remains diagnostic-only until a real complete kernel
  build and trace validate it. Capture alias/provenance and clean the mount.
- Short-source probe `41801581346`, head `9f76036`, passed all 4,000 native
  actions and verified 5,463 declared outputs. The failing QCA recipe shrank
  from 135,239 to 80,479 bytes; no E2BIG remains. Fresh raw Image, Image-dtb,
  vmlinux, System.map, .config, all four base DTBs and Crux overlay are present
  in one fresh producer receipt. Source bytes were not edited, and `/k` was
  cleaned. The 14 pre-existing headers-install shell-probe diagnostics remain
  visible; the header action and complete native worker returned success.
  The new production context admits this exact pinned runtime policy only for
  the kernel-output owner, without trace or diagnostic lineage. The complete
  production build will restart all 39 tasks from empty current-run output
  state; neither prior successful compiled outputs nor probe Image are reused.
- Current production run `41808162966` has passed bootstrap and dispatched wave
  0 under worker revision `a4fb476`. All dependencies in this run point only to
  this run's new producer artifacts. No general cross-run resume was enabled.
- The repaired production kernel passed. Wave 2 exposed a host-launcher runtime
  omission: `w002-s000` failed because d8 could not find `host/linux-x86/framework/d8.jar`.
  The successful kernel task had freshly produced that JAR as a declared output,
  but exported only the launcher, not the JAR. D1 adds explicit generated runtime
  transport metadata, not a warm JAR. The same 39 capsule archives/Ninja actions
  stay unchanged. Producer extra exports must already be declared native outputs;
  consumers import only a current-execution receipt of the correct owner. Later
  production restarts remain empty-state, with no reuse of this failed cohort.
- Runtime transport admission in `41818784799` stopped before compilation on
  a same-slice xsdc/xsdc.jar pair. Same-owner runtimes now require a first Ninja
  invocation for those already declared local output targets, after the original
  cold/bundle/import guards, followed by the complete original task target list.
  This explicit intra-task order uses only newly computed outputs in one worker;
  no capsule/Ninja bytes or source inputs change, and no foreign JAR is admitted.
- New empty-state run `41819689572` passed bootstrap. Its admitted context
  derives 23 runtime bundle relationships across d8, manifest-merger,
  r8-compat-proguard, metalava, sdkparcelables, class2nonsdklist and xsdc.
  Three producer tasks receive declared extra-export lists; xsdc has a local
  JAR prebuild target in `w002-s006`. The original 39 capsule byte identities
  remain unchanged. Integrated validation: 449 tests, 16 skips, actionlint pass.

## Executor protocol

1. Before editing, read the current plan and task revisions, actual dependency
   outputs, worktree status, and relevant code, then run the card's entry check.
   Run entry checks and validation commands exactly as written, and read each
   exit code from the command result instead of appending `echo $?` or other
   commands. Stop and hand back with the evidence when the check fails or when
   the task's allowed paths or listed code facts changed beyond the recorded
   baseline, accepted dependency outputs, and this task's own recorded
   checkpoint diff. Changes elsewhere need no action. Preserve existing changes;
   if their source cannot be distinguished, hand back, and never overwrite or
   reset them. A permitted adjustment must be named in the task card; “keep it
   equivalent” is not permission to redesign.
2. Start new work only from `ready`, with dependencies and authorization checked.
   If the card delegates readiness checks, the executor may mark a settled
   `draft` task `ready` after recording evidence that every listed prerequisite
   holds; unresolved design choices still go to the planner. After step 1,
   resume an interrupted `active` task without resetting its status. For a
   `blocked` task, record evidence that the blocker was resolved within the
   contract and existing authority, then return to `ready` if unstarted or
   `active` if resuming. Design, acceptance, or drift blockers remain with the
   planner. Status changes do not authorize contract changes.
   Choose only the listed local details. Do not change requirements,
   architecture, acceptance, or the planner-owned contract. Record work in the
   result area, and also update the task's status where the plan records it,
   such as its status field or heading, to `active`, `blocked`, or `done` as
   observed.
3. Run the specified validation and record each command with its exit code and
   output. Fix and rerun in-contract defects; restore the environment within
   existing authority; adjust commands only inside an explicitly allowed binding
   range. Do not ignore failures or lower pass criteria. Stop, record the
   attempts, and report the handback or escalation the card names when a change
   outside the allowed paths or fixed implementation seems necessary, a failure
   is not an in-contract defect (an environment problem outside existing
   authority, a command binding, a plan or design error, a requirement gap, or a
   pre-existing failure), or the attempt budget is spent.
4. Handback states the disproven assumption, observation and source, affected
   task and acceptance, attempts and results, and decision needed. The executor
   cannot edit the contract to accept its own work.
5. After interruption, record the last validation actually run and its result,
   unfinished diff, current code state, and next action. On resumption repeat
   step 1. Mark `done` only with the stated evidence. After the final
   integration task is `done`, hand its evidence to the acceptance owner; only
   an accepting record completes the overall request.
