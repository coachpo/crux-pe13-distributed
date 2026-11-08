import json
from pathlib import Path
import unittest

CONFIG = Path(__file__).parents[1] / "config"


class SourceConfigTest(unittest.TestCase):
    def test_manifest_carries_every_override_field(self):
        """The freeze reads the manifest; each override must already be applied there."""
        manifest = {project["path"]: project
                    for project in json.loads((CONFIG / "cloud-source-manifest.json").read_text())["projects"]}
        overrides = json.loads((CONFIG / "cloud-source-overrides.json").read_text())["projects"]
        for path, fields in overrides.items():
            with self.subTest(path=path):
                self.assertIn(path, manifest)
                for key, value in fields.items():
                    self.assertEqual(manifest[path].get(key), value, key)

    def test_patches_exist_with_their_digests(self):
        import hashlib
        for project in json.loads((CONFIG / "cloud-source-manifest.json").read_text())["projects"]:
            for patch in project.get("patches", []):
                with self.subTest(patch=patch["path"]):
                    data = (CONFIG.parent / patch["path"]).read_bytes()
                    self.assertEqual(hashlib.sha256(data).hexdigest(), patch["sha256"])

    def test_kernel_short_source_policy_follows_the_kernel_pin(self):
        """The compile refuses short-source mode unless both policy copies name the pinned kernel."""
        import sys
        sys.path.insert(0, str(CONFIG.parent / "scripts"))
        import cloud_compile
        pins = {project["path"]: project["revision"]
                for project in json.loads((CONFIG / "cloud-source-manifest.json").read_text())["projects"]}
        runtime = json.loads((CONFIG / "standard-build-runtime.json").read_text())["kernel_source_alias"]
        self.assertEqual(runtime, cloud_compile.SHORT_KERNEL_SOURCE_POLICY)
        self.assertEqual(pins[cloud_compile.SHORT_KERNEL_SOURCE_PROJECT], cloud_compile.SHORT_KERNEL_SOURCE_REVISION)


if __name__ == "__main__":
    unittest.main()
