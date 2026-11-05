# Fresh standard-runner PE13 build

Plan: standard-runner-build / revision 12 / active (2026-11-05).

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
| V1 | done | root + read-only observer | Cold execution `42018017251`, worker `9300985475998818d5938a55d11ee9ed03732d83`, passed attempt 1: all 39 native jobs, all eight waves, final R8/NOTICE/images and successful producer summary. No previous compiled output was admitted. |
| V2 | active | root + read-only observer | Assembly/validation `42059779500` uses only the verified `42018017251` context, summary and producer artifacts. Real gates, unpublished artifact, compact evidence and usage report remain required. |
| K1 | done | frontend_decomposition + root | Per-string overflow measured; identical source alias experiment passed complete kernel build. Validated pinned adapter promoted to production. Diagnostic artifacts are excluded from production summary and assembly. |
| D1 | done | source_footprint + root | Declared-output host JAR transport and same-task prebuild ordering passed all seven real wave-2 tasks in `41819689572`. These outputs are not reusable by a later empty-state production execution. |
| D2 | done | source_footprint + root | AIDL source closure repaired and all 39 capsules passed real cloud repacking in `41835375815`. Root verified the new registry preserves every source/graph/index/shard/plan identity and retained it separately from the immutable original. Native validation of the repaired inputs remains V1. |
| A3 | done, real gate pending V2 | acceptance_audit + root | Existing Recovery cpio gates now consume the verified fresh recovery image's extracted ramdisk when no standalone `ramdisk-recovery.img` was produced. Reuses the supported extraction helper; valid/invalid synthetic cpio regressions pass without weakening the fstab requirement. |
| A4 | implemented, real gate pending V2 | artifact_retry + root | Property report mismatches now fail assembly rather than being only recorded. Standard assembly verifies all delivered SHA256SUMS and streams decompression to compare native system-image byte size/SHA before artifact retention. Existing image and FIT gates remain required. |
| D2-perf | done | source_footprint + root | Optimized replacement pack `41857485715` passed all 39 tasks and registry, with unchanged source/graph/index/plan pins. It is preparation evidence, not the production ROM input: compile `41866735013` uses the first repaired registry `41835375815`. |
| T1 | done | frontend_decomposition + root | Bounded transport-only retry/backoff passes fixtures. Same-execution attempt 2 recovered `w002-s003` on a clean standard runner, preserving worker/context and importing only this run's fresh parents. Wave-3 scheduling resumed; no cross-run output reuse. |
| T2 | done | artifact_retry + root | Shared local composite retries artifact upload once after ten seconds, preserving terminal upload/native failures. Applied across the standard preparation/build chain. Real standard-runner fixture `41902415401` passed retry/replacement-byte verification and propagation when both uploads fail. |
| D3 | done | rust_graph_review, rust_dependency_audit + root | New cold `41920576338` passed all prior native stages and the formerly failing `w006-s001` keystore2 link. Declared-owner Rust native-file transport is verified; no native source, flags or frozen graph changes were needed. |
| D4 | done | rust_graph_review, rust_dependency_audit, license_source_pack, notice_source_guard + root | All 39 source capsules repacked, then real `41965423848` final task passed local metadata prebuild, both NOTICE XML outputs and system/vendor NOTICE files. This proves NOTICE, not the later final R8/image actions. |
| D5 | done | rust_graph_review, rust_dependency_audit + root | Hosted source-only `42001383332` added the pinned SystemUI config. Actual final native task `127336015735` of `42018017251` passed R8, NOTICE and remaining system/vendor image actions. |

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
- Run `41819689572` passed all 23 tasks in waves 0–2, including the repaired
  generated Java runtimes. Wave 3 `w003-s000` then failed resolving AIDL types
  such as `android.hardware.graphics.composer3.Capability`. Its declared `-I`
  root maps correctly to the pinned source package; the collector follows only
  explicit imports while AIDL's resolver also opens referenced types without
  import declarations. D2 verifies the capsule inventory before accepting that
  diagnosis. Healthy sibling jobs continue so other concrete failures are not
  hidden. A source closure repair changes capsule archive identities, not source
  revisions, slice manifests, Ninja actions, build identity or the 39-task plan.
  Any repaired registry is admitted as a new, separately evidenced capsule
  revision; the accepted original preparation registry remains unchanged. The
  subsequent production run starts all 39 tasks empty and cannot reuse outputs
  from this failed run or diagnostic probes.
- The old `w003-s000` capsule sidecar confirms the AIDL diagnosis: it contains
  30 files in the frozen composer3 API-1 package, including IComposer and
  IComposerClient, but omits Capability and other implicitly referenced sibling
  types. Inspecting the cloud-generated metadata does not execute any local PE
  preparation or build action.
- Pre-assembly review found an independent input-shape gap: the fresh graph
  produces `recovery.img`, not standalone `ramdisk-recovery.img`. FIT preparation
  already supports extracting its ramdisk, but the earlier cpio gate requires
  the missing standalone file. A3 moves/reuses fresh extraction before that gate
  and must retain every existing Recovery cpio requirement.
- D2 source closure fixtures and integration passed: 451 tests, 16 environment
  skips, actionlint and whitespace checks. The source-only closure also resolves
  unimported fully-qualified types across declared source import roots, including
  nested types, without scanning generated OUT siblings. Capsule revision
  admission now binds each reviewed pack run to its own checked-in registry;
  unreviewed runs and changed registries are rejected. Cloud repacking is next.
- Cloud repack `41835375815`, head `19ed549f5161da859a7dede62a23e81fec45b1b2`,
  is regenerating the 39 cold source capsules from the same admitted graph,
  index, shards and public source snapshot. No native compiled inputs are seeded.
  A3 integrated validation passed 453 tests (16 environment skips), actionlint
  and whitespace checks; real image/cpio acceptance remains V2.
- Repack `41835375815` reached 38/39 successful capsules without failure.
  `w002-s002` remained in collection for over 100 minutes. Inspection identifies
  redundant AIDL name/root/path resolution: the final leaf fallback searches
  the shard-wide include-root union again, and sibling recursion repeats
  qualified-name probes. D2-perf caches these searches without omitting the
  fallback or reducing source closure. The existing task continues while that
  optimization is reviewed; no successful preparation evidence is discarded.
- D2-perf integration passed 456 tests (16 environment skips), actionlint and
  whitespace checks. Root selected a parallel replacement full repack with the
  optimized collector rather than relying on the slow outstanding task. Both
  use the same 39 slice manifests, source pins and build identity; only a fully
  successful, reviewed capsule registry can enter the new cold compilation.
- The first repair run `41835375815` completed all 39 pack jobs and wrote a
  successful aggregate registry (artifact `13340737858`, ZIP digest
  `9d83be8688c1b545304ff7411a6098dfea42231969d7ed9043c44658ddc86775`).
  Fourteen archive identities changed; 25 are byte-identical. Source inputs sum
  to 141,033,865,445 bytes, archives to 40,834,867,107 bytes. Root verified the
  original source manifest, source/graph/index/shard runs, pruned-index identity,
  plan and exact 39 IDs are unchanged. Only the source closure and archive/input
  totals differ. The reviewed registry is checked in under
  `docs/evidence/standard-fresh-build-2026110303/` and selected by the compile
  workflow; no previous native outputs enter the next production execution.
  The optimized replacement repack `41857485715` remains an independent check;
  pending-only dispatch `41857418346` was cancelled before any work after the
  old build-only concurrency key delayed it. Preparation concurrency now also
  includes the worker revision, keeping independent revisions isolated.
- New production run `41866735013` admitted the repaired registry and passed
  all 13 native tasks in wave 0. Its exact worker revision is
  `79c8c2f13e879803e8e15e919d17a19520b2b4d1`; subsequent dependencies bind only
  to this run's receipts. No previously compiled object, JAR, kernel or image
  from any earlier execution is admitted. Wave 1 has started; later compilation
  and the separate image assembly/validation remain pending.
- Production `41866735013` passed all 16 tasks in waves 0–1, including its newly
  rebuilt kernel. Wave 2 `w002-s003` job `126734850823` failed before native
  compilation while downloading a GitHub artifact: HTTP 503, Azure `ServerBusy`,
  "Egress is over the account limit." Existing transport handles primary API
  quota resets but not transient 5xx. T1 adds bounded transport-only backoff.
  Healthy sibling jobs continue. Root may rerun this failed job and its skipped
  descendants in the SAME execution/worker SHA after the active attempt ends:
  the retry restores a new empty source/OUT runner, imports only already fresh
  parents from this execution, and overwrites the failed task's artifacts.
  This does not enable cross-run resume, old compiled inputs, ccache, native
  error suppression, or changing the pinned graph/source/capsules.
- T1 integrated validation passed 460 tests (16 environment skips), actionlint
  and whitespace checks. HTTP 429/500/502/503/504 have at most five transient
  retries with exponential backoff, honoring numeric/date Retry-After; existing
  primary quota reset waits and cross-host credential stripping are unchanged.
  This follows [Azure's storage throttling guidance](https://learn.microsoft.com/en-us/azure/storage/blobs/scalability-targets).
  [GitHub reruns retain the original commit/ref](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/re-run-workflows-and-jobs?tool=cli):
  production retry therefore retains worker `79c8c2f` and the same context;
  the transport fix applies to future executions and the later assembly job.
- The first compile attempt ended with 22 successful native tasks and only the
  pre-Ninja HTTP-503 failure. GitHub rejected failed-job reruns until its
  top-level state became completed; no live native task was cancelled. Root then
  reran failed jobs in this execution, retaining worker `79c8c2f`. Attempt 2
  `w002-s003` job `126758016193` passed from a new empty runner, with native
  compile/retention complete at 18:15:10 UTC. All 23 tasks in waves 0–2 now
  succeeded, and wave-3 plan `126768048702` started. The retry's successful
  artifacts replace its failed artifacts; every other producer remains freshly
  built within this same admitted execution.
- Wave 3 passed all six tasks, including formerly failing `w003-s000`; AIDL
  source closure is now verified by the real native build. Wave 4 boot-image
  owner `w004-s000` and siblings `s001`/`s003` passed. `w004-s002` job
  `126789890368` and `w004-s004` job `126789890461` failed before Ninja with the
  same storage HTTP 503. Root preserved both logs and waited for top-level
  completion (19:29:08 UTC) before the same-execution failed-job rerun. Attempt
  3 retains worker `79c8c2f`, empty task roots and only this run's fresh parents.
  There are 32 successful native tasks, two transport retries and five later
  tasks still required. No native error has been suppressed.
- Attempt 3 `w004-s002` job `126810964596` completed its native worker
  successfully in 885 seconds, but the upload action failed finalization with
  ECONNRESET after sending all 86,696,677 bytes. Its fresh receipt uploaded,
  but that alone cannot advance the failed workflow gate. T2 adds a bounded
  same-payload upload retry for future workflows and assembly. Current recovery
  must still cold-retry this task under the original worker SHA after the active
  attempt ends; no unfinalized archive or standalone success receipt is accepted
  as a usable producer. The failed job log is preserved separately.
- Attempt 3's other wave-4 retry, `w004-s004`, passed. Root verified that the
  upload failure was the only remaining failure before starting attempt 4 in the
  same execution/worker. It must rebuild the affected task cold and retain a
  finalized archive; the prior standalone success receipt is insufficient.
- T2 reuses the pinned upstream upload action and its existing defaults, without
  changing artifact paths, names or native checks. Only the first upload may
  continue to a delayed retry; the second fails normally. Retry overwrite is
  scoped to the same current-run artifact name, whose prior finalization may
  have succeeded despite a lost response. The first inner upload uses always()
  so caller-requested failure diagnostics remain available. This uses supported
  [composite step conditions and error handling](https://docs.github.com/en/actions/reference/workflows-and-actions/metadata-syntax).
  Integrated 460 tests (16 skips), actionlint and whitespace checks pass. Real
  standard-runner transport fixture execution remains required before T2 closes.
- Live transport fixture `41902415401`, commit
  `a08c14c6322f2734233441298ae94b13d33a153c`, passed on standard Ubuntu. A forced
  first-attempt 409 conflict was followed by the ten-second delayed overwrite;
  finalized replacement artifact `13358971537` replaced seed `13358356896`, and
  the helper ID, downloaded artifact and replacement bytes matched. A separate
  missing-file case failed both attempts and propagated failure to the caller,
  whose explicit regression assertion passed. The expected error annotations
  belong only to this transport fixture, not native compilation. Root inspected
  the raw job `126828060381` log and successful workflow conclusion.
- Attempts 4–6 recovered the wave-4 upload failure and wave-5 pre-Ninja HTTP
  503 failures, all with empty retried task roots and unchanged worker/context.
  All 36 native tasks through wave 5 ultimately passed. Wave-6 `w006-s001` job
  `126865695045` then genuinely failed linking keystore2: its literal Rust
  `-C link-args` requires the Soong stripped `libkeystore2_apc_compat.so`, owned
  and freshly built by `w005-s000`, but that producer exported only the installed
  copy. The full 19,921-character native command matches frozen edge 919 exactly.
- Independent read-only audits of all 39 manifests found 318 Rust edges and
  656 literal generated native-file occurrences: 513 earlier-wave and 143 local,
  with no missing, same-wave foreign or later-wave owners. Every foreign owner
  is already a direct plan dependency. Three unexported consumer/path pairs are
  APC compat (`w006-s001` from `w005-s000`) and gRPC/grpc_wrap
  (`w005-s001` from `w004-s002`); thirteen other underdescribed pairs happen to
  be exported already. D3 admits these literal reads generically via the existing
  fresh runtime transport contract, not a library-name special case. Local
  native ordering remains unchanged: the failed task's local km_compat library
  demonstrably built before Rust linking. The canonical parser/index and all
  39 capsule byte identities remain unchanged, so no reindex/repack is needed.
  The next production execution nevertheless recompiles all 39 tasks empty;
  none of `41866735013`'s compiled outputs can seed it.
- D3 implementation covers ordered split/joined codegen options, single
  link-arg versus space-list link-args, wrappers/cwd, linker flag values and
  declared self outputs. It does not evaluate shell payloads, infer basename
  providers, read warm files or silently expand unknown response files. Literal
  generated inputs must have an exact selected owner; foreign owners must be in
  earlier waves. Existing current-run receipt/spec checks verify their bytes.
  Repeated consumer/path relations are deduplicated. The
  [Rust codegen option contract](https://doc.rust-lang.org/rustc/codegen-options/index.html#link-args)
  and recorded native command provide the operand semantics. Integrated
  validation: 476 tests, 16 environment skips, actionlint and whitespace checks.
- Old execution `41866735013` ended with 37 successful tasks and the known
  keystore2 native failure; its healthy Recovery-image task completed before
  the pending replacement acquired concurrency. New cold run `41920576338`,
  worker `9b37b3140ba9792a79105956d711beaa1c8dddd0`, passed bootstrap and wave-0
  planning. It retains the same repaired 39 capsule archives and immutable
  preparation pins, but imports no previous native archive. Root inspected the
  cloud-generated context and verified all three formerly missing Soong native
  libraries have both declared-owner extra exports and matching current-run
  consumer runtime imports. Original Ninja/local ordering remains unchanged.
- Execution `41920576338` passed all 38 tasks through wave 6, including D3, on
  attempt 1 without transport failures. Final `w007-s000` job `126984058689`
  failed at Vendor NOTICE generation: xmlnotice recursively opened missing
  `group_gen_odm/meta_lic`, selected and built by `w003-s002` but not exported.
  The full recorded command matches the native failure. No NOTICE check is
  disabled and no prior generated metadata may supplement the failed task.
- D4 audits identify 17,650 selected metadata outputs and 28 actual notice
  readers. Three image metadata writers execute exact declared Bash rsp scripts;
  command-only source-read analysis treated them as terminals and also missed
  copied metadata content provenance. The two XML readers require 13,051
  unique metadata files: 12,602 foreign and 449 local. All owners are selected,
  all foreign owners are earlier direct dependencies; 2,226 paths need exports.
  Therefore graph/index/slicing and native command bytes need not change.
- The same XML closure needs 843 SOURCE license texts. The admitted final cold
  capsule contains only 379: 464 logical paths are missing, including LICENSE
  -> README for wpa_supplicant. The additional pinned bytes are 4,330,867;
  repacking must preserve the relative link and its target. SOURCE file hashes
  and full archive verification remain mandatory, with no generated OUT seed.
  Current receipt inspection found 13,192 exported metadata files all regular
  and nonempty; no runtime type/empty-file exception is needed.
- D4 contracts: the pure license resolver follows recorded Go/Make arguments,
  exact declared shell scripts, copy content provenance and recursive -d;
  descriptive fields do not become reads. Packing validates every consulted
  writer manifest against the accepted inventory before adding SOURCE texts.
  C0 admits foreign metadata through existing current-run byte-verified runtime
  imports and prebuilds same-task metadata targets before original full Ninja
  targets. C1 validates required notice sources immediately after cold restore
  and caches per-producer receipt/spec lookup without relaxing existing checks.
  Real metadata preflight runs on the standard pack-plan job before any source
  mounts. Full SOURCE-only repack uses the existing 39-job hosted workflow;
  after its registry is reviewed, native compilation restarts all 39 empty.
  Integrated fixture validation currently passes 506 tests (16 skips), actionlint
  and whitespace checks. This is not real NOTICE or ROM acceptance.
- SOURCE repack `41953667023`, worker
  `1f5e55886094f853c9ae799e8c7e63189ef816b8`, passed real standard-runner
  metadata preflight before source mounts. Whole `w007-s000` has twelve readers
  (two XML plus ten HTML), requiring 14,320 metadata inputs, 715 local metadata
  targets and 894 SOURCE texts. The earlier 13,051/449/843 counts refer only to
  the two-XML diagnostic scope; the additional HTML texts already existed, so
  the missing SOURCE byte count is unchanged. All seven notice-bearing shards
  passed the selected-owner/wave checks, and the unchanged 39-job pack matrix
  started without a failure.
- The new final SOURCE capsule is already verified: 9,265 entries (465 new
  entries for 464 missing logical paths and the relative symlink target),
  350,432,341 archive bytes, SHA
  `d5e094c9cdb710570611a6c1590df8e6f315e4ee97dd38dc5f5582e2dabc2678`.
  Its slice manifest SHA remains
  `d161f2966f2c07b3e885333b41f368035ebcb5f4a6fb26e1487ad7c8a2d312cb`.
  Pack receipt lists all 894 notice sources. Root inspected this cloud receipt;
  the remaining pack jobs and aggregate registry are still required before
  admitting the new inputs to native compilation.
- SOURCE-only repack `41953667023` completed successfully on attempt 1, all
  39 pack jobs plus aggregate summary. Registry artifact `13387065833` is
  admitted separately and becomes the new compile default. Root verified all
  immutable identities and exact 39 IDs against the prior registry. Archives
  total 40,835,226,805 bytes; source inputs total 141,038,057,987 bytes. Sixteen
  archive identities changed (including source-closure provenance and the
  earlier AIDL lookup optimization); no generated OUT data is admitted.
  The graph, pruned index, slice manifests, plan and original native commands
  remain unchanged. Full integrated validation is 506 tests (16 skips),
  actionlint and whitespace checks. C0/C1 notice admission and required-source
  checks now activate atomically with this reviewed complete source registry.
- New production `41965423848` passed real bootstrap with worker
  `11d1a111305b5ae2bac53dfabab60cc7a97402ed`. Root inspected its cloud context:
  the final task requires 894 SOURCE texts, prebuilds 715 declared local
  metadata targets, and imports foreign metadata only from current-run owners.
  The formerly missing group_gen_odm metadata now has both the exact
  `w003-s002` extra export and matching `w007-s000` runtime import. All source,
  graph, index, slice, plan and native command identities remain pinned;
  native compilation starts empty for the complete 39-task cohort.
- `41965423848` passed all 38 earlier jobs on attempt 1. Final `w007-s000`
  job `127129322030` passed 856/856 prebuild actions and generated the system
  and Vendor NOTICE artifacts, proving D4. At main action 470/484, the frozen
  SystemUIGoogle `r8-compat-proguard` command then failed reading the nested
  SOURCE include `frameworks/base/packages/SystemUI/proguard.flags`. Its
  parent vendor config is present, but the exact 6,956-byte child is absent
  from the admitted final capsule. No later system image is accepted.
- D5 read-only audit inspected all 39 manifests and actual pinned config bytes:
  89 compat invocations, 1,444 literal `-include` operands, 51 SOURCE roots
  and 53 config files. The final task needs 12 SOURCE configs and has 11.
  Only the SystemUI child is absent; it has no further include. All generated
  direct configs have selected owners and the failing invocation parsed its
  65 OUT configs before reaching the vendor SOURCE config. Native reader
  arguments/ownership stay unchanged. No additional missing input is evidenced
  among the 14 unreported actions; they still require real native verification.
- Existing SOURCE analysis recognized only Java R8 main with `--pg-conf`,
  while packing treated top-level `-include` as generic C/C++ input without
  following the config's own directives. Repair actual reader scopes, not a
  hardcoded filename. The [pinned R8 parser](https://r8.googlesource.com/r8/+/464c2760a67392dacdd4c0b327fb46127278f014/src/main/java/com/android/tools/r8/shaking/ProguardConfigurationParser.java#405)
  resolves include relative to current base and initializes each child at its
  own parent; class/member annotations are not configuration includes.
- Independent assembly review found that `verify_images()` returns a failed
  property report on mismatch while `gate_images()` previously only recorded
  it. A4 rejects that status with the mismatch details. The standard workflow
  also checks final `SHA256SUMS`, decoder success, and the decompressed system
  image's size/SHA against its pre-compression identity. Scoped unit checks and
  synthetic transport cases passed; no actual PE image was processed locally.
- D5 reuses a single R8 SOURCE config walker for source-read analysis and
  capsule packing, including actual recorded/embedded invocation scopes. It
  separates compatibility config arguments from modern input/output flags,
  respects declared response grammar and base directories, and excludes OUT
  contents and class/member annotations. Integrated 517 tests (16 environment
  skips), actionlint and whitespace checks passed. Full hosted SOURCE-only
  repack is next; graph/index/slices remain frozen and all native tasks will
  start empty again. No diagnostic or production archive from earlier failed
  executions will enter the new native cohort.
- SOURCE-only `42001383332`, worker
  `48006655505395174101c2d0d071f0d895d07455`, passed metadata preflight, all 39
  pack jobs and registry artifact `13409183863` on attempt 1. The final capsule
  (artifact `13403359258`) has 9,266 entries; root inspected its actual sidecar
  and confirmed exactly one added file, the 6,956-byte pinned SystemUI config
  with SHA `c28876c7814086825d4f1f9bb418793c884c426fed85996c4d06310f695412b9`.
  Its archive SHA is
  `a5dbfab4c58e4005258d6bacd1c4cd463b1945b40feb3eded955f75ad95ca31b`;
  the slice manifest SHA and allowed-generated-inputs-empty contract remain
  unchanged. The separately reviewed registry becomes the compile default.
- Comparing the latest pack registry to the D4 registry, only `w006-s000`
  and `w007-s000` archive identities changed. Aggregate SOURCE inputs grew by
  exactly 6,956 bytes; archived inputs total 40,835,238,357 bytes. Graph,
  source, index, pruned-index and plan bindings are unchanged. New full cold
  execution `42018017251` passed bootstrap on worker
  `9300985475998818d5938a55d11ee9ed03732d83` and started all thirteen wave-0
  tasks. It cannot use any compiled archive from the earlier attempts.
- V1 completed successfully in `42018017251`, attempt 1, at 15:51:12 UTC
  on 2026-11-05. All 49 workflow jobs passed, including 39 native tasks and
  the summary. Final native job `127336015735` passed the complete task.
  Root downloaded and checked summary artifact `13431450207`: all 39 bindings
  refer to this exact execution and worker, using pack `42001383332` and the
  unchanged source/graph/index/slice identities. No rerun, prior compiled
  outputs, local build or larger/self-hosted runner entered this cohort.
- V2 assembly/validation `42059779500` was dispatched after V1 was confirmed
  completed/success, with `build_run=42018017251`. It must finish with all
  image, property, AVB/v1, cpio, FIT and delivery integrity gates and a verified
  Actions artifact; the compile success alone does not accept the delivery.

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
