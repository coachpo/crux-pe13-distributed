# PE13 preparation on standard GitHub-hosted runners

`freeze-standard.yml` is the validation workflow for replacing the local PE13
build-preparation environment. Every job uses the public repository's standard
`ubuntu-22.04` runner. The accepted v3 ROM delivery remains separate.

The workflow fetches the complete pinned projects in
`config/cloud-source-manifest.json`, creates 32 SquashFS source layers, mounts
them read-only below a writable overlay, bootstraps the host tools and runs
`m --skip-ninja bootimage recoveryimage systemimage vendorimage` with a fresh
external `OUT_DIR`. It imports neither an existing Android Ninja graph nor local
host binaries. The generated graph, source receipts, build log and resource
samples are retained as Actions artifacts. The current gate stops after graph
generation; index/slice/capsule stages will be enabled after this gate passes.

## Dispatch

```sh
gh workflow run freeze-standard.yml --repo coachpo/crux-pe13-distributed \
  --ref main -f build_number=2026110302 -f gogc=50
```

An empty `build_datetime` selects the run's UTC start time. `gogc` is explicitly
injected into the Soong child process because its normal `env -i` launcher
discards inherited environment settings. The patch exists only in the runner's
writable source overlay and is included in the graph receipt.

To retry with reusable source layers after a partial failure, set
`reuse_sources_from=<run ID>`. A layer is reused only if its pinned projects and
owned copy/link layout are unchanged, its original receipt matches the source
run's exact manifest, and every image part passes size and SHA-256 verification.
Changed or unavailable layers are rebuilt from their cloud sources. A complete
source run can instead be selected with `source_run=<run ID>`; that skips the
source matrix and requires every layer to match the current manifest.

## Source identity

Project archives come from exact public Git revisions. Failed Google Gitiles
archives fall back to a depth-one fetch of the same revision and `git archive`.
The two former PE GitLab projects use their retained Internet Archive Git
bundles. Maintained Crux forks are selected explicitly in
`config/cloud-source-overrides.json`.

The device tree revision `80c137e` is reconstructed from published parent
`e606ed7` and `config/source-patches/device-crux-dev-v2.patch`. The full resulting
Git tree must match `e5779e581e6f49b590dc655ac7947e466be4d061`; this preserves
the v2 fstab source without publishing a new branch in the device repository.

The pinned snapshot excludes Git object databases, old build outputs and local
diagnostic experiments. Repo-generated copy/link entries, including links that
the resolved manifest omitted after project replacement, are explicit inputs.
Updates to source revisions, patches or layout require a new source manifest.

## Evidence

- `cloud-source-<nn>`: compressed source parts, project/content identities and
  `layer.json`; reused layers also preserve the original receipt.
- `failure-source-<nn>`: project-specific source acquisition failures.
- `cloud-graph-<build>`: graph identity receipt, exact host-source GC patch,
  `time -v` build output, two-second memory/disk samples and the generated out
  archive on success.

A passing fixture test or successful source layer does not establish graph
generation. The graph gate requires a successful hosted invocation and all four
expected Ninja graph files from the fresh out directory.
