# Standard hosted runner build preparation

Plan: standard-runner-freeze / revision 6 / active (2026-11-04).

The owner authorized implementation, commits/pushes and Actions execution to
replace local PE13 build preparation using only public-repository standard Linux
GitHub-hosted runners. No larger/self-hosted runners or device operations.
Baseline: `a134139812b700c6667343f3f4aeabb87aee0064` in
`out/crux-pe13-actions-ota-2026-10-11/actions-repository`; clean working tree.
The remote main revision matches. Preserve the accepted v3 delivery and inputs.

The first acceptance gate is a fresh four-image Soong/Kati graph generated on a
standard runner from a complete pinned source view mounted from compressed source
layers, with no locally compiled host binaries or pre-generated Android graph.
Next gates are a new canonical index/closure/ownership/shard plan and parallel
cold-input capsule packing. Source acquisition must run in Actions; local source
inspection may export revision and layout metadata only. Missing cloud source
bytes are an explicit blocker, never replaced with placeholders or old graphs.

## Contracts

All executors read this plan, applicable AGENTS.md and the unchanged executor
protocol linked below. Entry check: `git diff --quiet -- <allowed paths>` before
editing. Root owns integration, workflows, source manifest publication and final
acceptance. Executors may edit/test their assigned files; root alone commits,
pushes, triggers runs and updates this record. No unrelated source or device
changes. Use the inherited model/effort. After three in-contract fix/check cycles,
return any remaining failure to root with evidence; never lower acceptance.

| Task | State | Owner | Allowed paths / interface / validation |
| --- | --- | --- | --- |
| S1 source layers | done | source_footprint + root integration | `scripts/cloud_source.py`, `tests/test_cloud_source.py`; pinned JSON projects (path, URL, revision), copy/link entries and deterministic layer IDs; fetch complete project trees, preserve symlinks/modes, SquashFS layers and machine receipts; tests cover manifest/layout/nested-project handling and source failure. Accepted source run `41677764699`. |
| F1 graph frontend | done | frontend_decomposition + root integration | `scripts/cloud_freeze.py`, `tests/test_cloud_freeze.py`; fresh external OUT_DIR from complete mounted source; explicit identity, GOGC injection under env reset and `--skip-ninja` four-image graph only; no existing graph or local binary seeds. Accepted graph run `41677764699`. |
| W1 graph integration probe | done | root | workflow, pinned manifest, docs; standard `ubuntu-22.04` only, complete-source layer fanout, graph job and always-uploaded logs/receipts; accepted hosted run `41677764699`. |
| P1 index/slice/pack | active | root | Preserve canonical producer/scope semantics, audit new graph ownership and package all shards in parallel. Overall acceptance requires successful hosted slicing, cold probes, every planned capsule and aggregate registry. |
| P1-I canonical index | done | frontend_decomposition + root integration | `scripts/cloud_index.py`, fixture tests and `prepare-standard.yml`; accepted graph run `41677764699` and the same source manifest. All four graph digests validated; a new canonical SQLite index and four target producers accepted in run `41685367061`. Actual index/closure work runs only on standard runners. |
| P1-A/S/R runtime contracts | active | source_footprint, frontend_decomposition, postgraph_decomposition; root integrates | New source actor, source-reader and host/Rust runtime modules. Generate from the current graph/source tree; no old audit JSON or diagnostic binaries as runtime inputs. Root `cloud_shard.py` applies producer-bound order dependencies to a copied fresh index, cold-probes source-built Go actors and scalar rematerialization, then prunes and shards. |

## Execution and acceptance

- S1/F1 source/helper diffs inspected and accepted for the first hosted probe.
  Source manifest: 1,170 pinned projects, 32 layers and 51 copy/link entries.
- Local checks: actionlint 1.7.12 passed; 312 unit tests completed with 15
  skips; `git diff --check` passed. SquashFS mounting/build execution remains a
  hosted acceptance gate, not established by fixture tests.
- Actions graph gate: pending; retain run IDs, commit IDs, raw logs and artifact
  receipts under the workspace task evidence directory.
- First hosted probe: `41664251881`, commit `b988133`. Standard runner source
  layers were produced successfully; other layers exposed upstream-vs-maintained
  fork URL drift, the local-only device v2 commit, and Gitiles 503/truncated
  archives. The next probe uses corrected maintained repository URLs, an exact
  source patch with a checked Git tree for device commit `80c137e`, and a
  commit-verified Git fetch fallback for failed Gitiles archives. Unchanged source
  layers may be reused only after complete layer-contract and part verification.
- Integration decision: pending. This plan remains active until every gate passes
  or a concrete external/source/runner blocker is established and reported.
- Revised source probe: `41666229218`, commit `dafe3a4`, build `2026110302`.
  It has produced the patched device tree and most source layers; source-07
  exposed two existing dangling QCOM layout links. Commit `1cf0910` preserves
  those links. Follow-up `41668368879` is queued behind that probe, with the same
  build identity and verified reuse from `41666229218`.
- Execution owners: root integrates, commits, pushes and dispatches. Source,
  frontend and source-reuse helpers were delivered by reused `gpt-6-luna`
  subagents; root independently inspected the diffs and ran the unit/actionlint
  checks. The source/graph observer is read-only. Source VM/device trees remain
  unchanged; local inspection exported revision/layout metadata and one existing
  source patch only. No local graph generation or source capsule packaging ran.
- Current graph acceptance target: full run `41674662924`, commit `9ea420e`,
  build `2026110302`, with verified source reuse from `41666229218` plus the
  parallel source-07 repair `41673377043` (`mode=source-only`). The source-only
  run cannot satisfy graph acceptance. Pending runs `41668368879` and
  `41673697025` were intentionally canceled with zero jobs when superseded.
  No generated graph has passed the gate yet. Before graph execution, CI
  authentication is removed from the Android child environment so it cannot
  enter Soong's persisted available-environment snapshot.
- Superseding active target: full run `41677764699`, commit `154c1dc`, build
  `2026110303`. It combines verified source layers from `41666229218`,
  `41673377043` and `41664251881`; the first run's source-01 artifact became
  available before the duplicated second download. The complete 32-layer input
  selection totaled 32,311,000,918 artifact bytes including receipts/ZIP framing.
  Source-01's old/current layer contracts were independently compared and match.
- Source gate accepted: all 32 source jobs plus plan succeeded in
  `41677764699`. Graph job `126065932319` is active, initially restoring and
  mounting the compressed source view. This accepts source acquisition and
  transport, not graph generation or the overall plan. Superseded pending run
  `41674662924` was canceled with zero jobs. Current local suite: 331 tests,
  15 skips, actionlint passed. P1 remains draft until the hosted graph succeeds.
- Fresh graph gate accepted: `41677764699` succeeded, graph job
  `126065932319`, artifact `cloud-graph-2026110303` ID `13254050598`,
  800,535,901 bytes. The mounted source images total 31,927,148,544 bytes.
  The graph command completed in 7:28.79 with maximum RSS 14,700,164 KiB and
  exit 0. The full graph job (transport/setup/graph/archive) took about 22 minutes.
  This is a fresh hosted graph, not a frozen-graph replay. Canonical indexing is
  now ready and is being connected through `prepare-standard.yml`.
- Canonical index gate accepted: run `41685367061`, commit `9c89b47`, job
  `126089581715`, artifact `13255112960`. 1,749,436 edges / 1,961,774 outputs
  / 2,441,929 statements across four fresh Ninja files; all four target producers
  exist. SQLite is 8,154,189,824 bytes, compressed to 204,799,099 bytes. Indexing
  took 5:42.37, maximum RSS 285,180 KiB. Restored graph resource samples show
  maximum swap use 5,517,811,712 bytes and minimum available disk 61,768,339,456
  bytes during generation. Runtime completion/slicing is the next hosted gate.
- First shard gate `41698738039` failed on the fresh native scalar command
  `/bin/bash -c "touch .../soong/build_number.txt"`. The next revision unwraps
  that single native writer before validation and adds regression coverage.
  Source-reviewed kernel/DTBO/header PATH consumers are rebound by output paths
  to fresh index IDs; their source-built interposer and alias-directory readiness
  is explicit. Distinct dependency categories retain their union in both SQLite
  and emitted Ninja bindings. Parallel capsule jobs restore only indexed graph
  text from the derived graph transport, not warmed frontend binaries.
- Pack/full workflow stages and a streaming per-capsule archive verifier are
  implemented; actual pack acceptance remains pending a successful slice gate.
- Follow-up full run `41759299116`, commit `d0726f7`, reused the accepted
  graph/index and mounted all 32 layers. Native scalar rematerialization passed,
  and the four kernel/DTBO/header PATH consumers resolved to fresh producer IDs.
  Source-read analysis then failed on Make's manually written license argument
  response file (edge `106927`). The pinned Make source writes it within the
  same command using `rm`, `touch`, and literal `echo -n` appends, rather than
  declaring Ninja `rspfile_content`. The fix must derive those bytes from the
  native command without opening any warm OUT file. The pinned Go license tool
  expands only top-level `@` operands; it does not recursively expand response
  payload tokens. These semantics, unknown-writer rejection and source-read
  behavior require fixture regression tests before another hosted full run.
  This failed run is not shard or pack acceptance. Raw logs and receipts are
  retained under `out/crux-pe13-standard-freeze-2026-11-03/` in the workspace.
- Run `41762955760`, commit `766c68f`, passed the source-read derivation and
  reached bulk dependency updates, then failed with SQLite `database is locked`.
  A large write transaction can spill dirty pages and exclude a newly opened
  reader. The fix batches and closes raw-statement reads before any updates,
  retaining the dependency union and unchanged canonical graph parser. A
  constrained-cache fixture reproduces the failure before the fix and passes
  afterward. Index/shard log pipelines now use `pipefail` so the native failure
  cannot be hidden by `tee`; failed receipts already prevented downstream
  acceptance. The next full run still requires actual slice/pack acceptance.
- Independent review of `14cb7cd` found that pre-reading also caches no-op
  candidates; only edges with actual changes may enter the published adaptation
  profile. A regression reproduced the missing-reason `KeyError`, then passed
  after publishing only reason-bearing changes. Run `41764768391` was
  intentionally canceled during setup rather than spend further source-download
  work on the superseded revision. No shard/pack acceptance is claimed for it.

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
