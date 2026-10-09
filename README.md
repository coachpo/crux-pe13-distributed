# Distributed Crux PixelExperience 13 build experiment

This repository owns an experiment to compile the Crux PE13 ROM on multiple
standard public GitHub Actions Ubuntu x64 runners, then assemble its image
outputs. It does not change the maintained device, platform, kernel or U-Boot
repositories and does not operate a phone.

The build inputs use the published October 8, 2026 Crux source selection. The
existing generated Android Ninja graph is a planning input: compilation is
performed from source in cold runner tasks. An existing installed ROM binary is
never a substitute for a successful distributed build.

The experiment is not yet a completed ROM build. The standard runner probe and
the cold common/library compilation trial succeeded. The full build now uses
41 dependency-ordered tasks across nine waves, covering 136,014 commands,
including a cold source build of the `bpglob` graph helper. The first 13 main
compilation tasks were dispatched together in [wave 1](https://github.com/coachpo/crux-pe13-distributed/actions/runs/37854864171).
All thirteen succeeded across the original run and its
[four-task retry](https://github.com/coachpo/crux-pe13-distributed/actions/runs/37857642143).
Verified assembler inputs and the native Python binding corrected four failures;
the other nine successful producers were reused.

The next wave has two verified successful tasks. Its remaining task exposed
transitive Rust libraries that were omitted at a shard boundary. A
[cold 38-command Rust supplement](https://github.com/coachpo/crux-pe13-distributed/actions/runs/37875108284)
now succeeds, with all 38 exported members verified and zero annotations.
The repair retains the original commands and restores their original ordering
after an imported `std` library cuts the dependency path to a locally rebuilt
`core` library. Evidence is in
[`rust-transitive-runtime.json`](results/diagnostics/rust-transitive-runtime.json).

The [cold frontend metadata repair](https://github.com/coachpo/crux-pe13-distributed/actions/runs/37883012817)
also succeeded with zero annotations. Three frozen scalar writers and the
unchanged native build-property command produced four verified outputs,
including build number `1791434921` and the corrected incremental version.
Future consumers must explicitly import those outputs and replace the two
older metadata files while retaining the other verified producer outputs.
Final image property checks remain required before packaging.

The remaining task in the second wave then exposed a missing transitive
FlatBuffers source include. Its [current retry](https://github.com/coachpo/crux-pe13-distributed/actions/runs/37887234891)
adds that source file to the cold capsule without changing the native graph or
the previously verified source inputs. A successful task status alone does not
certify complete ROM images.

After updating Actions dependencies to Node.js 24, the repeated resource probe
succeeded with zero annotations. Actual resource receipts are in
`results/resource-probe/`; the successful compilation exchange is recorded in
[`results/pilot/`](results/pilot/).

## Pipeline

1. Freeze source revisions and index the generated graph without compiling.
2. Export small Ninja slices, preserving their commands and variable scopes.
3. Archive each slice's frozen source and declared toolchain inputs. Restore
   eligible toolchain directories from exact public source revisions and verify
   them against the same frozen input identities.
4. Run dependency-ordered waves on `ubuntu-22.04`. A shared dependency is built
   once and transferred through successful task artifacts.
5. Verify producer receipts and assemble `boot.img`, `recovery.img`,
   `system.img` and `vendor.img` into an image-build archive.

`scripts/graph.py` performs indexing/slicing; `scripts/capsule.py` creates input
archives; `scripts/scheduler.py` dispatches waves; `scripts/worker.py` executes
cold tasks; `scripts/assemble.py` packages the four required images.

The first real compilation trial built two independent native shared-library
consumers, `libbase` and `libz`, with one shared prerequisite task. Both consumers
imported the identical successful common artifact. Every exported archive
member matched its receipt, and both resulting libraries are AArch64 ELF files.
Success of this trial alone does not establish full-ROM compilation.

No accepted installed images or old compiled objects are supplied as cold
compilation inputs. Vendor blobs and toolchain prebuilts remain declared source
dependencies. `CCACHE_DISABLE=1` prevents reuse of an existing compiler cache.
The accepted full plan contains no imported generated-metadata approvals.

Current Actions dependencies are `actions/checkout@v7.0.1` and
`actions/upload-artifact@v7.0.2`, both using Node.js 24. Android's source/toolchain
revisions stay pinned to the PE13 build configuration.

Runner jobs also use `AdityaGarg8/remove-unwanted-software@v5` to remove the
preinstalled .NET, Android SDK, Haskell, CodeQL and Docker images. Package
removal, general tool-cache deletion and swap removal are disabled. The ROM
uses its declared source/toolchain inputs; disk records retain the actual
before-cleanup and after-cleanup capacity.
The [cleanup probe](https://github.com/coachpo/crux-pe13-distributed/actions/runs/37905466509)
succeeded with zero annotations and observed 21.74 GiB of additional available
space, leaving 107.95 GiB free. Its measurements are in
[`results/runner-cleanup/`](results/runner-cleanup/).

## Validation

Run the Python regression suite on Linux with Ninja and zstd installed:

```sh
python3 -m unittest discover -s tests -v
```

Standard public runners provide 4 vCPUs and 16 GB RAM, and individual jobs have
a six-hour limit. The resource probe observed about 86 GiB of free disk, which
is an observation for that runner image, rather than a platform guarantee.
See the official [runner specifications](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)
and [Actions limits](https://docs.github.com/en/actions/reference/limits).

The final archive is a development image build, rather than a signed OTA or
device-validated release. Android kernel handoff in the Crux development setup
uses the maintained U-Boot chain. This experiment performs no device writes.
