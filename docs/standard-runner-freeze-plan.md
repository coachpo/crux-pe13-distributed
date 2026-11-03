# Standard hosted runner build preparation

Plan: standard-runner-freeze / revision 3 / active (2026-11-03).

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
| S1 source layers | ready | source_footprint | `scripts/cloud_source.py`, `tests/test_cloud_source.py`; pinned JSON projects (path, URL, revision), copy/link entries and deterministic layer IDs; fetch complete project trees, preserve symlinks/modes, SquashFS layers and machine receipts; tests cover manifest/layout/nested-project handling and source failure. No workflow edits. |
| F1 graph frontend | ready | frontend_decomposition | `scripts/cloud_freeze.py`, `tests/test_cloud_freeze.py`; mount source layers at their recorded paths, fresh external OUT_DIR, explicit build identity, `--skip-ninja` four-image graph only; no graph restore or local binary seeds. Report subprocess failures and graph/resource evidence. Explicit GOGC injection under env reset if needed. No workflow/source manifest edits. |
| W1 integration probe | active | root | workflow, pinned manifest, docs; standard `ubuntu-22.04` only, complete-source layer fanout, graph job and always-uploaded logs/receipts; local unit suite plus actual Actions acceptance. |
| P1 index/slice/pack | draft | root | Start after usable generated graph and full source-layer receipts exist. Preserve canonical producer/scope semantics, audit new graph ownership and package shards in parallel. |

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
