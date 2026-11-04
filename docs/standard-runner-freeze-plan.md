# Standard hosted runner build preparation

Plan: standard-runner-freeze / revision 7 / done (2026-11-04).

## Outcome and scope

The complete preparation chain is accepted on public-repository standard
`ubuntu-22.04` GitHub-hosted runners: public pinned source acquisition,
compressed source mounts, fresh Soong/Kati graph generation, canonical indexing,
runtime/dependency completion, closure-preserving pruning, slicing and parallel
cold-input capsule packing. No larger/self-hosted runner, local PE graph/index,
local slicing/packing, pre-generated local graph or locally compiled host seed
was used. Local work comprised implementation, source/layout metadata and
evidence inspection, and fixture tests.

The owner authorized implementation, commits/pushes and Actions execution.
The maintained checkout is `out/crux-pe13-actions-ota-2026-10-11/actions-repository`
in the Crux workspace, published as `coachpo/crux-pe13-distributed`.
The accepted implementation revision is
`9b4e3e24d360685a6e0eecfdd91ad206de3531ce`; final documentation adds evidence
without changing that implementation. The original baseline was `a134139`.

This accepts preparation for the pinned `aosp_crux-userdebug` development
product, not a newly compiled ROM, image assembly, release/OTA publication or
device deployment. The accepted `dev-pe13-2026101105-v3` delivery, kernel/symbol
pairings and device state remain unchanged.

## Completed contracts

| Task | State | Owner | Accepted interface and evidence |
| --- | --- | --- | --- |
| S1 complete source | done | source_footprint + root | 1,170 pinned projects, 32 SquashFS layers, 51 copy/link entries; cloud acquisition/reuse verifies project contracts and image parts. Complete source run `41677764699`. |
| F1/W1 fresh frontend | done | frontend_decomposition + root | Fresh external OUT_DIR, complete compressed source view, explicit build identity, GOGC injection under env reset and four-image `--skip-ninja`; graph job `126065932319` in `41677764699`. |
| P1-I canonical index | done | frontend_decomposition + root | Four fresh Ninja digests verified, new SQLite index and four terminal producers; run `41685367061`. Canonical parser unchanged by later fixes. |
| P1-A/R source runtime contracts | done | source_footprint, frontend_decomposition, postgraph_decomposition + root | Source-derived scalar/Go actors, license/R8 reads, host-tool libraries, Rust imports and structural PATH dependencies; real cold probes and dependency fixed point passed. No old audit JSON, native IDs or diagnostic binaries imported. |
| P1-S prune and slice | done | root | Full source/pruned closure equivalence and producer-bound metadata projection; 136,591 edges / 136,566 actions, 39 shards / 8 dependency waves. Successful shard job `126375516290` in `41769467686`. |
| P1-P parallel cold packing | done | frontend_decomposition + root | All 39 jobs and aggregate registry passed in `41779197896`; exact inventory/manifest/index bindings and streamed tar contents verified, no generated-OUT allowlist. |
| E1 delivery | done | acceptance_audit + root | Usage guide, acceptance record and compact repository evidence; raw API records, logs and receipts retained in the workspace evidence directory. |

Executors had bounded file contracts and no remote-write authority; root
reviewed/integrated changes, committed, pushed and dispatched Actions. No
separate worktrees or task branches were created. Historical failed-run evidence
is retained; no source VM or device changes require cleanup.

## Accepted compatible run chain

Build `2026110303`, epoch `1793724452`; source-manifest SHA-256
`2950cc77ceea78dd47d140e611ef38e477823f306d841433b72f167e0ff1c69d`.

| Gate | Accepted run/job | Result |
| --- | --- | --- |
| Source + fresh graph | `41677764699` / graph `126065932319`, head `154c1dc` | 32 verified source images totaling 31,927,148,544 bytes. Native graph command 7:28.79, max RSS 14,700,164 KiB; peak swap 5,517,811,712 bytes; minimum free disk 61,768,339,456 bytes. Artifact `13254050598`. |
| Index | `41685367061` / `126089581715`, head `9c89b47` | 1,749,436 edges / 1,961,774 outputs / 2,441,929 statements, four files and all four target producers. SQLite 8,154,189,824 bytes; 5:42.37, max RSS 285,180 KiB. Artifact `13255112960`. |
| Runtime completion + slicing | `41769467686` / shard `126375516290`, head `3802881` | Scalar and seven-action Go cold probes passed; dependency additions +63,494, +44, +0. Native preparation 34:39.00, max RSS 12,741,012 KiB. Pruned index 1,534,316,544 bytes. Artifact `13296156332`, 610,880,810 bytes. |
| Parallel packing + registry | `41779197896`, head `9b4e3e2` | 39/39 pack jobs and summary succeeded; 43 total jobs = 41 success + 2 intentionally skipped upstream stages. Archives total 40,834,624,242 bytes, shard input bytes total 141,030,747,546 (includes cross-shard duplication). Max native pack RSS 2,276,388 KiB, max pack elapsed 1,081.518 seconds. Registry artifact `13300960503`. |

The earlier full run `41769467686` succeeded at the accepted shard job but failed
after its pack job was skipped. It is not described as a successful whole run.
The successful final pack-only run exercises resume with both upstream stages
intentionally skipped. It consumes the same accepted graph/index/shard identity.

Plan digest:
`0adc8fc5ee03139e5918ecb6ea33cbc2048f955721cafe5567f75ee15f28df71`.
Pruned index digest:
`52b19246e25a2ca94b74b9d0b70a7553eca3798d9aca91f4561733b91bdf94eb`.
All 39 receipts match those identities and the exact slice manifest inventory.
The artifact API lists all 39 capsule artifacts, all 39 receipt artifacts and
the successful aggregate registry. Every executed final job has the standard
`ubuntu-22.04` label and GitHub Actions runner group; the repository is public.

## Fixed failures and acceptance boundaries

- Source probes `41664251881` / `41666229218` exposed fork URL drift,
  unavailable local-only device revision, Gitiles failures and existing dangling
  layout links. Public pinned forks, exact device patch/tree verification, Git
  fetch fallback and verified multi-run layer reuse resolved them.
- `41698738039` exposed native scalar `bash -c touch`; its one writer now
  rematerializes the exact frontend bytes and mode.
- `41759299116` exposed Make's literal in-command license response writer.
  Recovery follows the pinned tool's one-level response grammar without reading
  a warm OUT response file.
- `41762955760` exposed SQLite read/write locking during cache spill. Batched
  reads now close before writes; no-op cached candidates do not enter adaptation
  profiles. Superseded `41764768391` was canceled during setup.
- `41765283807` passed cold probes but exposed out-of-closure runtime metadata
  during pruning. Projection preserves retained edge/rule semantics and drops
  irrelevant ownership; full closure equivalence is checked.
- `41769467686` exposed implicit status propagation from intentionally skipped
  ancestors. Explicit `!cancelled()` gates now dispatch resumed stages and allow
  ordinary cancellation.
- `41774690617` finished 38 successful capsules and one false input-audit
  failure. The bpglob heuristic had scanned a later `mkdir -p` operand.
  Invocation-scoped arguments fix the cause, preserve real source glob reads,
  and do not approve any generated OUT input. The final run revalidated all
  39 capsules on the fixed collector, including `w000-s012`.

Validation: `python3 -m unittest discover -s tests` passed 403 tests with 16
environment skips; actionlint 1.7.12 and `git diff --check` passed. Local fixtures
are separate from the real hosted gates above.

## Delivery and retention

- [Usage](standard-runner-freeze.md) — full and resumable workflow commands.
- [Acceptance](standard-runner-preparation-acceptance-2026-11-04.md) — scope,
  results, identities and artifact pointers.
- [Compact evidence](evidence/standard-preparation-2026110303/) — source artifact
  identities, graph/index/shard receipts, slice inventory, all condensed pack
  receipts, capsule artifact identities, runner audit and aggregate registry.
- Workspace raw evidence: `out/crux-pe13-standard-freeze-2026-11-03/`,
  especially `accepted-graph-41677764699/`,
  `accepted-index-41685367061/`, `accepted-shards-41769467686/` and
  `pack-41779197896/`.

Source artifacts expire after 7 days; graph/index/shard/capsule/registry artifacts
after 14 days. Compact acceptance evidence is versioned in Git; raw logs and
receipts are archived locally for inspection only, not required runtime inputs.
Expired payloads can be regenerated in Actions from the pinned public sources
and workflow inputs, subject to continued source/archive availability.

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
