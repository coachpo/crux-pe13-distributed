# Cold shared-dependency compilation trial

The common prerequisite and two native library consumers completed on separate
standard public `ubuntu-22.04` jobs. Both consumers downloaded the same successful
common artifact; neither was seeded from the existing local Android OUT tree.

| Task | Run | Commands | Checked outputs | Worker elapsed time | Export archive |
| --- | --- | ---: | ---: | ---: | ---: |
| common | [37844277125](https://github.com/coachpo/crux-pe13-distributed/actions/runs/37844277125) | 1,094 | 1,104 | 14.94 s | 3,758,305 bytes |
| libbase | [37851472557](https://github.com/coachpo/crux-pe13-distributed/actions/runs/37851472557) | 117 | 120 | 15.37 s | 92,913 bytes |
| libz | [37851472557](https://github.com/coachpo/crux-pe13-distributed/actions/runs/37851472557) | 43 | 44 | 3.36 s | 55,141 bytes |

Worker elapsed time excludes job provisioning, host dependency installation and
source/toolchain transport. It is not an end-to-end ROM timing estimate.

Independent verification checked all 23 exported archive members against their
producer receipts, including type, mode, content digest and device symlink
target. The consumer receipt identities refer to the exact same common manifest,
receipt and archive. `libbase.so` (251,152 bytes) and `libz.so` (102,088 bytes) both
have little-endian 64-bit AArch64 ELF headers. The machine-readable evidence is
[`verification.json`](verification.json).

Earlier attempts exposed missing source symlink chains, Go test fixtures, host
GCC runtime inputs and Python 2 aliases. Consumer attempts also exposed external
device symlink handling and an unset `OUT_DIR`. These transport/environment
issues were corrected before the successful runs. No source compilation errors
were suppressed.

This trial establishes cold compilation and verified sharing for these selected
libraries. It does not certify a complete ROM, a signed OTA, hardware boot or
device functionality. The full ROM task uses its own frozen graph, input plan
and fixed worker commit.
