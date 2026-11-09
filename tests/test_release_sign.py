from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import crux_zip  # noqa: E402
import release_sign  # noqa: E402


class ReleaseSignTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.key, self.public = root / "key.pem", root / "public.pem"
        subprocess.run([crux_zip.openssl(), "ecparam", "-name", "prime256v1", "-genkey", "-noout",
                        "-out", str(self.key)], check=True)
        subprocess.run([crux_zip.openssl(), "ec", "-in", str(self.key), "-pubout", "-out", str(self.public)],
                       check=True, capture_output=True)
        self.manifest = root / "payload/crux-install.txt"
        self.manifest.parent.mkdir()
        self.manifest.write_text("schema=crux-install/1\nkind=zip\ntarget_build=2026110950\n")

    def test_signature_verifies_with_the_pinned_public_key(self):
        with mock.patch.object(release_sign, "PUBLIC_KEY", self.public):
            signature = release_sign.sign(self.manifest, self.key.read_text())
        crux_zip.verify(self.manifest, bytes.fromhex(signature), self.public)
        self.assertEqual(sorted(path.name for path in self.manifest.parent.iterdir()),
                         ["crux-install.sig", "crux-install.txt"])

    def test_key_that_is_not_the_published_install_key_is_refused(self):
        with self.assertRaisesRegex(crux_zip.ZipError, "does not verify"):
            release_sign.sign(self.manifest, self.key.read_text())

    def test_missing_environment_key_is_refused(self):
        with self.assertRaisesRegex(release_sign.SourceError, "is empty"):
            release_sign.sign(self.manifest, "  \n")

    def test_waits_for_the_dispatching_assembly_run_to_finish(self):
        states = iter([{"status": "in_progress"}, {"status": "completed"}])
        with mock.patch.dict("os.environ", {"GH_TOKEN": "token"}), \
                mock.patch.object(release_sign.release_source, "_json_api", side_effect=lambda *_: next(states)), \
                mock.patch.object(release_sign.time, "sleep") as sleep:
            release_sign.wait_until_completed("43000000002", 60)
        sleep.assert_called_once()


if __name__ == "__main__":
    unittest.main()
