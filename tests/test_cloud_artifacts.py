import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("cloud_artifacts", SCRIPTS / "cloud_artifacts.py")
cloud_artifacts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cloud_artifacts)


class CloudArtifactsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.destination = self.root / "downloaded"
        self.artifacts = {
            "cloud-source-02": {"id": 102, "name": "cloud-source-02", "expired": False},
            "cloud-source-00": {"id": 100, "name": "cloud-source-00", "expired": False},
            "failure-source-03": {"id": 203, "name": "failure-source-03", "expired": False},
            "expired-source-01": {"id": 201, "name": "expired-source-01", "expired": True},
        }

    def tearDown(self):
        self.temporary.cleanup()

    def transport_mocks(self):
        return (mock.patch.object(cloud_artifacts.cloud_layers, "artifact_inventory",
                                  return_value={name: item for name, item in self.artifacts.items()
                                                if not item.get("expired")} ),
                mock.patch.object(cloud_artifacts.cloud_layers, "_extract_artifact"))

    def test_exact_name_extracts_single_artifact_directly_into_destination(self):
        inventory_patch, extract_patch = self.transport_mocks()
        with inventory_patch as inventory, extract_patch as extract:
            result = cloud_artifacts.download_artifacts(
                "12345", "owner/repository", self.destination, "token", name="cloud-source-02")
        inventory.assert_called_once_with("12345", "owner/repository", "token")
        extract.assert_called_once_with(self.artifacts["cloud-source-02"], self.destination.resolve(),
                                        "token", "owner/repository")
        self.assertEqual(result, [{"name": "cloud-source-02", "id": 102,
                                  "destination": str(self.destination.resolve())}])

    def test_pattern_downloads_all_matches_into_named_directories_in_order(self):
        inventory_patch, extract_patch = self.transport_mocks()
        with inventory_patch as inventory, extract_patch as extract:
            result = cloud_artifacts.download_artifacts(
                "12345", "owner/repository", self.destination, "token", pattern="cloud-source-*")
        inventory.assert_called_once_with("12345", "owner/repository", "token")
        self.assertEqual([call.args[0]["name"] for call in extract.call_args_list],
                         ["cloud-source-00", "cloud-source-02"])
        self.assertEqual([Path(call.args[1]).name for call in extract.call_args_list],
                         ["cloud-source-00", "cloud-source-02"])
        self.assertEqual([Path(item["destination"]).name for item in result],
                         ["cloud-source-00", "cloud-source-02"])

    def test_missing_exact_name_and_pattern_fail_without_extracting(self):
        for selector in ({"name": "missing"}, {"pattern": "missing-*"}):
            with self.subTest(selector=selector):
                inventory_patch, extract_patch = self.transport_mocks()
                with inventory_patch as inventory, extract_patch as extract:
                    with self.assertRaises(cloud_artifacts.ArtifactDownloadError):
                        cloud_artifacts.download_artifacts(
                            "12345", "owner/repository", self.destination, "token", **selector)
                inventory.assert_called_once()
                extract.assert_not_called()

    def test_pattern_refuses_artifact_names_that_escape_destination(self):
        self.artifacts["../escape"] = {"id": 999, "name": "../escape", "expired": False}
        inventory_patch, extract_patch = self.transport_mocks()
        with inventory_patch, extract_patch as extract:
            with self.assertRaisesRegex(cloud_artifacts.ArtifactDownloadError, "Unsafe artifact name"):
                cloud_artifacts.download_artifacts(
                    "12345", "owner/repository", self.destination, "token", pattern="*")
        extract.assert_not_called()

    def test_missing_token_is_rejected_before_inventory(self):
        inventory_patch, extract_patch = self.transport_mocks()
        with inventory_patch as inventory, extract_patch as extract, \
                mock.patch.dict(cloud_artifacts.os.environ, {}, clear=True):
            with self.assertRaisesRegex(cloud_artifacts.ArtifactDownloadError, "GH_TOKEN or GITHUB_TOKEN"):
                cloud_artifacts.download_artifacts(
                    "12345", "owner/repository", self.destination, name="cloud-source-00")
        inventory.assert_not_called()
        extract.assert_not_called()

    def test_extraction_errors_surface(self):
        inventory_patch, extract_patch = self.transport_mocks()
        with inventory_patch, extract_patch as extract:
            extract.side_effect = RuntimeError("artifact zip is corrupt")
            with self.assertRaisesRegex(RuntimeError, "artifact zip is corrupt"):
                cloud_artifacts.download_artifacts(
                    "12345", "owner/repository", self.destination, "token", name="cloud-source-00")


if __name__ == "__main__":
    unittest.main()
