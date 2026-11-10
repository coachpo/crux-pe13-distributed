# PE13 fresh ROM — standard-runner acceptance

Historical acceptance only. The frozen branch, artifact defaults and re-run
commands below document that version, not a current alternative pipeline. Use
the [hosted operation guide](standard-runner-freeze.md) for new builds and stage
recovery. Preserve the original observations; publication and device acceptance
have separate evidence.

**Accepted on 2026-11-05:** a complete cold compile, assembly and image
verification of `aosp_crux-userdebug` succeeded using only standard
`ubuntu-22.04` GitHub-hosted Linux runners in the public
`coachpo/crux-pe13-distributed` repository. No local PE build, previous native
`OUT_DIR` products, ccache, larger runner or self-hosted runner was used.
The build and assembly workflows produce an Actions artifact without
publishing a Release or OTA. No device was flashed or boot-tested. The
[post-cleanup rerun](#post-cleanup-rerun-4423f08) is a separate host acceptance
record.

## Accepted runs and artifacts

| Stage | Evidence |
| --- | --- |
| Pinned source and fresh Soong/Kati graph | [41677764699](https://github.com/coachpo/crux-pe13-distributed/actions/runs/41677764699) — 1,170 projects, 32 compressed SOURCE layers |
| Canonical index | [41685367061](https://github.com/coachpo/crux-pe13-distributed/actions/runs/41685367061) — 1,749,436 edges |
| Slice and pruned index | [Successful shard job 126375516290](https://github.com/coachpo/crux-pe13-distributed/actions/runs/41769467686/job/126375516290) — 39 slices in eight waves; the later failure of that historical run did not accept its pack stage |
| Final cold SOURCE capsules | [42001383332](https://github.com/coachpo/crux-pe13-distributed/actions/runs/42001383332) — 39/39 verified, archive bytes 40,835,238,357; registry artifact `13409183863` |
| Fresh native compile | [42018017251](https://github.com/coachpo/crux-pe13-distributed/actions/runs/42018017251) — attempt 1, all 39 native tasks and all 49 workflow jobs successful, 4 h 31 m 55 s from run creation to completion |
| Assembly and image validation | [42059779500](https://github.com/coachpo/crux-pe13-distributed/actions/runs/42059779500) — attempt 1, all gates successful, 14 m 59 s from run creation to completion |
| Download | [cloud-fresh-rom-42018017251](https://github.com/coachpo/crux-pe13-distributed/actions/runs/42059779500/artifacts/13431937735) — 2,698,205,767-byte Actions ZIP; expires 2026-12-05 |

The graph and capsules embed build number `2026110303`; keeping that frozen
identity does not reuse any compiled output. The accepted compile worker is
`9300985475998818d5938a55d11ee9ed03732d83`. All 39 receipts bind that worker and
execution `42018017251`, the exact shard manifest, `CCACHE_DISABLE=1`, and only
declared producers from the same execution. The retained summary artifact
`13431450207` contains all 39 bindings. The source manifest SHA-256 is
`2950cc77ceea78dd47d140e611ef38e477823f306d841433b72f167e0ff1c69d`; the plan
SHA-256 is `0adc8fc5ee03139e5918ecb6ea33cbc2048f955721cafe5567f75ee15f28df71`.

## Image acceptance

The artifact's `delivery/` contains `boot.img`, `recovery.img`,
`system.img.zst`, `vendor.img`, `rom-cepheus.itb`, `recovery-cepheus.itb`,
`delivery-record.json` and `SHA256SUMS`. The independent assembly runner
read only verified producer artifacts from the successful compile. It checked:

- System/vendor and build identity properties against independent frozen graph
  expectations, with zero mismatches.
- Android v1 boot and Recovery headers and structural AVB metadata, including
  partition, size and fingerprint consistency. The AVB algorithm is `NONE`
  for boot and `SHA256_RSA2048` for Recovery. This is not a claim of a
  device-verified trust chain or boot.
- Boot cpio mount points and the fresh Recovery image's extracted cpio
  `system/etc/recovery.fstab`.
- One fresh producer for kernel `Image`, `vmlinux`, `System.map`, `.config`
  and the four base DTBs, with a fresh Crux overlay. Both FITs passed the
  source-declarative boot contract and embedded kernel/ramdisk/DTB checks;
  no old compiled reference DTB was used by fresh assembly.
- All seven final `SHA256SUMS` entries. Streaming decompression of
  `system.img.zst` matched the native `system.img`: 2,785,252,244 bytes,
  SHA-256 `4fb4d398655daf8d6ccbc23db2eda1f900e6e029591ec06196ae1fb56e40a73e`.

The accepted ZIP digest is
`d53e85f790c8b6919b9b453a982c367df2a144f5a5093db3f9f56dd697fa2efb`.
The artifact also retains the validation logs, receipts and FIT stage inputs.
The standalone FIT report is packaging-only; its source/hardware/release
verification flags remain false. Fresh source pairing is established by the
outer compilation and assembly producer receipts, not by those flags.
It does not generate the separate physical U-Boot Recovery-loader container.
The accepted device delivery `dev-pe13-2026101105-v3` is not replaced by this
host-only result.

## Post-cleanup rerun (`4423f08`)

**Accepted on 2026-11-06 (Helsinki):** both workflows ran at
[`4423f084417836e9f4281219c20eeef2989c5274`](https://github.com/coachpo/crux-pe13-distributed/commit/4423f084417836e9f4281219c20eeef2989c5274)
on standard GitHub-hosted `ubuntu-22.04` runners. The rerun kept the admitted
`2026110303` graph, shard plan and cold capsules, not prior compiled outputs.

| Stage | Rerun evidence |
| --- | --- |
| Cold compile | [42077474432](https://github.com/coachpo/crux-pe13-distributed/actions/runs/42077474432), attempt 1 — eight waves, 39/39 native tasks and 49/49 workflow jobs passed. The [summary artifact 13458161826](https://github.com/coachpo/crux-pe13-distributed/actions/runs/42077474432/artifacts/13458161826) contains 39 distinct same-run producer bindings at `4423f08`. |
| Assembly and image validation | [42121254314](https://github.com/coachpo/crux-pe13-distributed/actions/runs/42121254314), attempt 1 — `build_run=42077474432`, dispatched from `verify/pe13-standard-4423f08`. All seven admitted producers came from that compile. The four images and both FITs passed the applicable property, Android v1, AVB structure, cpio, source-declarative FIT and delivery integrity gates. |
| Actions delivery | [Artifact 13458249458](https://github.com/coachpo/crux-pe13-distributed/actions/runs/42121254314/artifacts/13458249458), `cloud-fresh-rom-42077474432` — 2,698,208,667-byte ZIP. |

An independent check downloaded the retained ZIP after assembly. Its byte
length and SHA-256
`c9f45996a2d14818ed6a67798867ba8b38798d529a9161e8ca88f75d11a6fe35`
matched the Actions artifact record. All seven `SHA256SUMS` entries (four
images, two FITs and `delivery-record.json`) matched the downloaded bytes.
Streaming decompression of the downloaded `system.img.zst` produced
2,785,252,244 bytes with SHA-256
`a542f1f54056db39ce94f68d55e95cd27d3c1616b8b0cf927576d33f146f496b`,
matching the native `system.img` identity recorded by this assembly.

The rerun is Actions-only: no Release, OTA publication or device test.
[`dev-pe13-2026110303-v1`](https://github.com/coachpo/crux-pe13-distributed/releases/tag/dev-pe13-2026110303-v1)
was published from the original assembly `42059779500` and artifact
`13431937735`, not from this rerun. The shared build number does not identify
the same bytes; the rerun does not replace the Dev v1 delivery or establish
device acceptance.

## Historical re-run procedure (frozen revision only)

In the repository's **Actions** page, run **Compile fresh PE13 on standard
runners** (`build-standard.yml`) on `verify/pe13-standard-4423f08` with the defaults
`pack_run=42001383332`, `shard_run=41769467686`, and
`build_number=2026110303`. Wait for the successful 39-task summary. Then run
**Assemble and verify a fresh PE13 build without publication** with `build_run`
equal to the new successful compile run ID, using the same branch. Download
`cloud-fresh-rom-<build_run>` from the successful assembly run.

Equivalent remote CLI controls (the commands dispatch jobs; they do not
compile or validate the ROM locally):

```sh
gh workflow run build-standard.yml --repo coachpo/crux-pe13-distributed --ref verify/pe13-standard-4423f08 \
  -f pack_run=42001383332 -f shard_run=41769467686 -f build_number=2026110303
# Wait for the new compile run to succeed; replace BUILD_RUN with its Actions ID.
gh run watch BUILD_RUN --repo coachpo/crux-pe13-distributed --exit-status
gh workflow run assemble-standard.yml --repo coachpo/crux-pe13-distributed --ref verify/pe13-standard-4423f08 \
  -f build_run=BUILD_RUN
# Replace ASSEMBLY_RUN with the successful assembly run's Actions ID.
gh run watch ASSEMBLY_RUN --repo coachpo/crux-pe13-distributed --exit-status
gh run download ASSEMBLY_RUN --repo coachpo/crux-pe13-distributed \
  --name cloud-fresh-rom-BUILD_RUN
```

Check that this branch still points to `4423f08` before dispatch. Do not use
`main` for the frozen inputs if its source manifest has advanced.
The defaults require the live graph artifact from `41677764699`, the shard
artifact from `41769467686`, and the registry and all 39 capsules from
`42001383332`. A compact receipt in Git is not a replacement for an Actions
payload. The graph artifact is scheduled to expire on **2026-11-17**.
Check availability before dispatch.
For expired preparation inputs or a new build identity, run a full freeze on
`main` (or let `nightly.yml` start one): the chain prepares, admits, compiles,
assembles and releases it, as
[standard preparation and admission](standard-runner-freeze.md#3-admit-preparation-for-a-cold-compile)
describes. Do not use an incomplete historical registry or a legacy reuse-state.

SOURCE artifacts are retained seven days, prep artifacts fourteen days, and
compile/assembly artifacts thirty days. Assemble before the compile inputs
expire. Standard runner compute and artifact storage are separate
considerations; refer to
[GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions)
for account-specific storage allowances.

## What the cold runs exposed and fixed

The execution record documents the Linux per-string Kbuild limit, generated
host Java runtime transport, implicit AIDL SOURCE reads, Rust native linker
inputs, recursive license/NOTICE reads and local metadata readiness, and
nested ProGuard SOURCE config reads. These were repaired without substituting
old compiled libraries or changing the frozen native command bytes. Transient
artifact reads now back off and uploads have one delayed retry; terminal
failures still propagate. The [live retry fixture](https://github.com/coachpo/crux-pe13-distributed/actions/runs/41902415401)
proved replacement after a first upload failure and rejection when both fail.
The accepted native run needed no manual rerun. Local fixture coverage was
517 tests (16 environment skips), plus actionlint and whitespace checks; real
build and image acceptance are the Actions results above.

See [the execution record](standard-runner-build-plan.md),
[delivery evidence](evidence/standard-fresh-build-2026110303/delivery-acceptance-42059779500.json),
[SHA256SUMS](evidence/standard-fresh-build-2026110303/SHA256SUMS-42059779500.txt)
and the [final integrity log](evidence/standard-fresh-build-2026110303/delivery-integrity-42059779500.txt).
