# Historical source-input records

P6 relocated these records from the active configuration/manifest directories
without changing their bytes. They retain their original source identities,
audit observations and artifact-pairing knowledge. Their dates/scopes are those
declared in the records, not the date of this move, and are not current source
or device acceptance.

| Original repository path | Historical location |
| --- | --- |
| `config/remote-source-profile.json` | [config/remote-source-profile.json](config/remote-source-profile.json) |
| `manifests/source-revisions.json` | [manifests/source-revisions.json](manifests/source-revisions.json) |
| `manifests/resolved.xml` | [manifests/resolved.xml](manifests/resolved.xml) |
| `manifests/crux-functional.xml` | [manifests/crux-functional.xml](manifests/crux-functional.xml) |

There is no runtime fallback or old-path alias. These files must not silently
select a source revision, transport, identity, toolchain or boot profile for a
new build. In particular, October source/revision observations do not identify
the current ROM or the live phone, and retired OTA/R6 references are not recovery
instructions. Do not restore retired objects from their historical descriptions.

Current A/B source pins are in
[config/cloud-source-manifest.json](../../../config/cloud-source-manifest.json),
with the maintained [hosted operation guide](../../standard-runner-freeze.md).
FIT packaging requires an explicit source-selection record for its actual
producers and the current source-declarative boot contract. Independent retained
DTB equivalence fixtures are separate from these source-input records and remain
test oracles, not packaging inputs.
