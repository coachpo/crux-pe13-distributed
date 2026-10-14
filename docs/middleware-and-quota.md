# Middleware, size and quota plan

The pipeline keeps every intermediate inside GitHub. This records what exists, where
it lives, how big it is and which limits apply, so a failure can be attributed to a
quota instead of guesswork. All sizes are the measured 2026101104 values.

## Where each artifact lives

| Artifact | Size | Carrier | Retention | Notes |
| --- | --- | --- | --- | --- |
| Closure-pruned graph index | 1.4 GiB raw, 53.6 MB zstd | `frozen-graph-2026101104-v1` release asset | immutable | `scripts/prune-index.py` output; `graph.py` reads it read-only |
| Ninja files `graph.py` re-reads | 9.2 GiB raw, 100 MB zstd | same release | immutable | 7 files; the archive stores whole seconds, so the run restores `mtime_ns` from `ninja-stats.json` |
| Shared prebuilt layer manifest | 10.7 MB | same release | immutable | keeps 29,819 toolchain files out of the capsules |
| Finalization audit inputs | 3.5 MB | same release | immutable | source closure, host runtime, path-interposer contract |
| Cold capsules | 3.03 GB total, 39 assets | `inputs-release-2026101104-v*` releases | immutable | largest single asset 495 MB, far below the 2 GB asset limit |
| Toolchain mirror | 1.7 GB, 11 assets | `toolchain-mirror-2026101104` | immutable | fallback for throttled public archives; the run normally restores from the pinned upstream archives |
| Retained signing toolchain | 1.5 GB zstd | `signing-tools-2026101104-v1` | immutable | host tools (3.7 GiB raw), verifier sources plus retained libraries, device packaging scripts |
| Compiler for the verifier rebuild | 622 MB | `toolchain-mirror-2026101104` | immutable | `clang-r450784d`, the same revision the retained binaries were built with |
| Worker artifact per shard | 0.1–5.5 GB | Actions artifact | 7 days | the wave workflow keeps its existing retention |
| Terminal target-files artifact | 4.5 GB | Actions artifact | 30 days | input of the signing run |
| Signed target-files | 5.46 GB | Actions artifact | 30 days | input of the publication run; never a release asset |
| Signed candidate delivery | OTA 1.69 GB, Recovery 134 MB, fastboot 1.94 GB | `signed-<build>` prerelease in the pipeline repo | durable | every asset is below the 2 GB limit; the publication run re-verifies each digest |
| Published OTA assets | same bytes | `coachpo/crux-pe13-ota` release | immutable | canonical download URL for the feed |

Nothing above 2 GB is ever uploaded as a release asset: the signed target-files stays
an artifact, and the largest public asset is the 1.94 GB fastboot rescue package.

## Runner resources

The standard public runner image offers 4 vCPU, 16 GB RAM and about 108 GiB free
space after the workflow's cleanup step (the cleanup is measured in
`results/runner-cleanup/`). The worst case in this pipeline is the signing job:
3.7 GiB host tools, a 0.3 GiB JDK, 12 GiB of extracted worker outputs, a 5.5 GiB
signed target-files plus staging copies. That stays well below the free space, and
every stage removes its temporary tree when it finishes.

The slice set a prepare run produces is about 7 GiB; a full run also keeps the
restored source tree (about 15 GiB) and the toolchain directories (7 GiB). A probe
run only holds the single shard it verifies.

## Quota and limit notes

* **Release asset limit 2 GB.** Enforced by keeping the signed target-files an
  artifact and by splitting nothing else: no planned asset approaches the limit.
* **Actions artifact limit 10 GB per artifact, 90-day default retention.** The
  pipeline sets 7 days for worker shards and 30 days for signed inputs and evidence,
  so a completed release does not accumulate artifacts indefinitely.
* **Actions cache.** The pipeline does not use the cache API; every run restores its
  inputs from immutable releases, which keeps runs reproducible.
* **Repository storage.** Releases in the pipeline repository hold the frozen inputs
  (about 6.5 GB) and the signed candidate assets (about 3.8 GB per candidate). Old
  candidates can be deleted once the corresponding OTA release exists, because that
  release carries the same bytes.
* **Job time limits.** No job exceeds the six-hour limit: the longest observed job is
  a full wave (about 90 minutes) and the signing job (about 60 minutes). The wave
  chain is a chain of jobs, so a full cold build can span more than six hours without
  any single job approaching the limit.
* **Network.** Runners reach GitHub, `android.googlesource.com` and `codeload.github.com`
  without the build host's restrictions. The toolchain restore keeps its jittered
  retry so a throttled archive host cannot fail every shard at once.

## What still needs the build host

* **A new release graph.** `graph.py index` needs the generated Ninja graph of a new
  product configuration, which only exists after a build-host `m` invocation. Its
  output is hosted once per release graph (`frozen-graph-<build>-v1`); everything
  downstream of it runs in Actions.
* **The retained host toolchain.** The signing toolchain is a build output of the
  tier 1 environment. It is hosted once and re-used unchanged; a toolchain change
  means a new `signing-tools-<build>-v1` release, uploaded from the build host.
* **The private keys.** They exist only as protected environment secrets and on the
  build host. The signing run materializes them into a private directory, uses them
  and removes them; no workflow writes them anywhere else.
* **Device work.** No workflow flashes, sideloads or otherwise talks to a device.
