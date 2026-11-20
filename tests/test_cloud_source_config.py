import json
from pathlib import Path
import unittest

CONFIG = Path(__file__).parents[1] / "config"


class SourceConfigTest(unittest.TestCase):
    def test_patches_exist_with_their_digests(self):
        import hashlib
        for project in json.loads((CONFIG / "cloud-source-manifest.json").read_text())["projects"]:
            for patch in project.get("patches", []):
                with self.subTest(patch=patch["path"]):
                    data = (CONFIG.parent / patch["path"]).read_bytes()
                    self.assertEqual(hashlib.sha256(data).hexdigest(), patch["sha256"])

    def test_kernel_short_source_policy_names_the_pinned_kernel_project(self):
        """The compile refuses short-source mode unless the admitted policy is its own and the kernel is pinned."""
        import sys
        sys.path.insert(0, str(CONFIG.parent / "scripts"))
        import cloud_compile
        pins = {project["path"]: project["revision"]
                for project in json.loads((CONFIG / "cloud-source-manifest.json").read_text())["projects"]}
        runtime = json.loads((CONFIG / "standard-build-runtime.json").read_text())["kernel_source_alias"]
        self.assertEqual(runtime, cloud_compile.SHORT_KERNEL_SOURCE_POLICY)
        self.assertRegex(pins[cloud_compile.SHORT_KERNEL_SOURCE_PROJECT], r"^[0-9a-f]{40}$")


if __name__ == "__main__":
    unittest.main()
