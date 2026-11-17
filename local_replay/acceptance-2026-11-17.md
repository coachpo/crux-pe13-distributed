# Isolated local cold replay acceptance — 2026-11-17

**Passed locally; diagnostic only.** Versioning this entry, its tests and this
record does not turn local results into A inputs. No production install key, delivery signature, Release,
self-hosted runner registration or phone operation was used.

## Fixed inputs and implementation

- Reference: accepted cold build2026111653, source-locking commit
  `f03b006e3df4f27c19cbf8d1cf8e94597ba28b2b`, Actions compile43932635374.
- Production implementation digest:
  `73adf4cf37938a9fa5fe26286dd6b6ea8363c9b2a0505b9df8076c5247a5822f`.
  The maintained Actions source/workflows/configuration were not changed for
  the local entry. Local-only code does not enter the A execution digest.
- Source-lock SHA:
  `96bdce2e8e2305fc7c5352bcf5344e6dad005b8df6dbcd08dad065bc60413ab8`.
- Ubuntu22.04/AMD64 containers in OrbStack; reported kernel
  `7.2.12-orbstack-00394-gbecdd6130068`. The Mac/Docker host is ARM64 and these
  AMD64 results are emulated diagnostics, not native GitHub-runner equivalence.
  No Rosetta/B source compatibility patch was applied.

Each task restored only its verified capsule into a new SOURCE/OUT namespace;
every complete declared predecessor archive was independently admitted. Native
execution was networkless with no host HOME, B source, B OUT/cache or full PE
source mount. Existing `worker.run_shard` performed the actual Ninja execution,
member verification and full export transport. `CCACHE_DISABLE=1` remained set.

## Actual cold executions

| Task | Scope | Result |
| --- | --- | --- |
| `w001-s004` |4,000-action Eigen/ordinary shard;4,028 declared outputs verified |pass |
| `w003-s000` |atomic kernel group, actual `/k` bind and paired symbols |pass |
| `w005-s002` |native ramdisk, file list and boot image |pass |
| `w006-s001` |complete atomic Recovery tree/CPIO/image/resource chain and vendor image |pass |
| `w007-s000` |actual system filesystem builder, listing and final image |pass |

Recovery consumed LOCAL Eigen and boot producer archives; system consumed the
LOCAL Eigen archive. No prior OUT directory was shared. Other predecessors
were genuine, verified outputs of the same fixed Actions context.

The separately rebuilt kernel is a standalone diagnostic with its own paired
symbols. Packaging deliberately used the **original single cloud kernel/DT/
symbol producer43932635374:w003-s000** and its matching derived inputs, not a
mixture of the newly rebuilt Image and old derived kernel copies/overlays.

## Complete unsigned assembly

The final isolated assembly passed existing identity/property, AVB, ramdisk,
kernel/DT/symbol pairing and FIT recipe gates. Installer13 host tests and static
AArch64 cross-build passed. Existing `crux_zip.prepare` generated an unsigned
payload staging object with build1653/fingerprint/marker, any-serial scope,
the pinned install-TWRP profile and discard-data policy.

`local-assembly.json` reports `status: success`, `diagnostic_only: true`,
`signed: false`, `installable: false`, `publication_admission: false`.
No delivery ZIP or delivery signature was generated. Installer tests use only
their existing disposable test fixtures. This is existing pre-signing staging,
not a new unsigned update format. The final assembly's immutable image ID is
`sha256:e911746cd6f3e165fbcf11602fb4b976b384761f4ee7de0cddbad8e7c815ee4a`.
Its actual runner/code snapshot is retained with the result.

The first assembly exposed a missing cross-libc development dependency in the
new local runtime, after image/FIT/host tests had passed. Adding the required
`libc6-dev-arm64-cross` package fixed the fresh retry without source patches,
changed flags or relaxed gates; the first failed diagnostic was preserved.

## Boundary regressions and evidence

- 14 targeted local-entry tests passed: exact predecessors, provenance,
  immutable implementation snapshot, kernel cleanup failure, environment
  credential exclusion, source pins and assembly mount boundaries.
- A real C++ smoke passed in the final frozen-code entry. A deliberately
  incomplete but internally hashed capsule failed on the missing nested header
  in a fresh container, demonstrating no ambient full-source fallback.
- Production receipt validation rejects LOCAL diagnostic receipts. No fake
  `GITHUB_*`, execution/fresh-build/reuse envelopes were generated.
- Before/after size and mtime of all11 retained B Ninja-state/success files
  match. This is an incremental-state readback, not a byte audit of whole OUT.
  The latest B cache remains build2026111002. Recovery inputs and device-accepted
  rollback records A0804/B0803 were not changed.

Workspace evidence lives under `out/crux-w10-2026-11-15/`:
`local-replay-*-input.json`, `local-replay-*-result/`,
`local-replay-assembly-result-v2/local-assembly.json`,
`local-replay-smoke/` and `local-replay-b-cache-preservation.json`.
Original artifact ID/ZIP-digest observations are in
`local-replay-inputs-1653/<artifact>/.artifact-origin.json`.

This acceptance covers the selected high-risk tasks and complete packaging
chain, **not a fresh local compile of every one of the46 shards**. Source/
index/shard/capsule preparation continues to use the existing production scripts
in a separate preparation environment; it is not another compiler or a local
runtime dependency of unattended Actions nightly.
