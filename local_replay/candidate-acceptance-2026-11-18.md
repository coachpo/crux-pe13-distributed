# 2026111653 changed-slice cold acceptance — 2026-11-18

**Passed locally for the standalone system file-list cut and complete unsigned
packaging.** This is diagnostic acceptance, not a hosted build, a full 47-task
cold build, a performance result, or a device acceptance.

## Inputs and cut

- Source lock `96bdce2e8e2305fc7c5352bcf5344e6dad005b8df6dbcd08dad065bc60413ab8`;
  32 source layers from run `43900583856`; successful freeze graph
  `43908333954` / artifact `14248300820`; canonical index
  `43910861904` / artifact `14248945388`. All 34 downloaded artifact ZIPs
  matched the API size and SHA-256; each source-layer receipt matched the
  successful freeze. Downloads were read-only and outside the containers.
- The preparation container regenerated the slices, independently audited and
  packed all 47 tasks, and checked packed inputs against the audits. The
  diagnostic-only cut puts `installed-files.txt` in its own task and adds one
  wave; it exists in the evidence snapshot, not in A's sharding profile.
  Plan SHA-256:
  `d20240b4bf0ce9a24a8f3d89f4a2166a8ae9c17146c15e413e5e32ae4e215a54`.
- Only the two system-tail task contracts changed relative to the accepted
  1653 Actions context. Native replays admitted 34 distinct archived producers
  from that genuine context only where the candidate's source and task
  contracts matched exactly. No historical OUT directory was mounted.

## Actual cold tasks

| Task | Scope | Result |
| --- | --- | --- |
| `w005-s002` | Ramdisk, list and boot image | Pass |
| `w006-s001` | Atomic Recovery and vendor images | Pass |
| `w007-s000` | Standalone system file list | Pass |
| `w008-s000` | System filesystem and final image, consuming the full LOCAL file-list archive | Pass |

All four tasks used fresh, networkless OrbStack AMD64 containers with
`CCACHE_DISABLE=1`. The final assembly used the unchanged cloud kernel/
DT/symbol producer `43932635374:w003-s000` with its matching downstream inputs.
The production receipt validator rejected all four LOCAL receipts.

## Complete unsigned assembly

Identity/property, AVB, ramdisk and paired FIT gates passed. All 13 host
installer tests and the static AArch64 installer build passed. The existing
`crux_zip.prepare` produced unsigned staging only. The final receipt reads
`status: success`, `diagnostic_only: true`, `signed: false`,
`installable: false`, `publication_admission: false`. No delivery ZIP,
delivery signature, cloud dispatch, Release or phone operation occurred.

## Limitations and evidence

The first preparation attempt was stopped after discovering that direct
audit/pack API callers must persist the returned receipts. The corrected entry
was verified with a fresh preparation. An alternate cut that moved the
file-list action into the heavy dexpreopt shard failed on the local runtime:
after adding Jammy headless Java 11 for R8's OS-PATH lookup, `dex2oatd64`
aborted on out-of-range pointers. A separate AMD64 container probe observed
`MAP_32BIT` return an address above 4 GiB. The passing cut leaves the
dexpreopt producer unchanged and uses its verified Actions archive; it does not
establish local dexpreopt success. No ART patch or relaxed admission was used.

Evidence under workspace `out/crux-w10-2026-11-15/`:

- `local-candidate-inputs-1653/downloaded-artifacts.json`
- `local-candidate-system-filelist-isolated-preparation-1653/preparation.json`
  and `context.json`
- `local-candidate-system-filelist-isolated-replay-1653/selection.json`,
  the four `native/receipt.json` files and `assembly/local-assembly.json`
- `local-candidate-system-filelist-replay-1653-jre11/w006-s000/native/build.log`
  for the failed dexpreopt attempt.
