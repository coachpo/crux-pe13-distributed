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
compilation tasks are running in [wave 1](https://github.com/coachpo/crux-pe13-distributed/actions/runs/37854864171).

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
