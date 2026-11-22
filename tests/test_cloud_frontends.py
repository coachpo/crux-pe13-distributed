import base64
import importlib.util
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import tempfile
import unittest


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ROOT = Path(__file__).resolve().parents[1]
cloud_frontends = load("cloud_frontends", ROOT / "scripts/cloud_frontends.py")
graph = load("cloud_frontends_graph", ROOT / "scripts/graph.py")


class CloudFrontendsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / "source"
        self.frontend_out = self.root / "frontend-out"
        self.destination_out = self.root / "destination-out"
        self.graph_dir = self.root / "graph"
        for relative in ("build/blueprint", "build/soong", "prebuilts/go/linux-x86"):
            (self.source / relative).mkdir(parents=True)
        for relative in (
            "build/blueprint/microfactory/microfactory.bash",
            "build/blueprint/bootstrap/bpglob/bpglob.go",
            "build/soong/cmd/path_interposer/main.go",
            "build/soong/ui/build/path.go",
            "prebuilts/go/linux-x86/bin/go",
            "prebuilts/go/linux-x86/pkg/tool/linux_amd64/asm",
            "prebuilts/go/linux-x86/pkg/tool/linux_amd64/compile",
            "prebuilts/go/linux-x86/pkg/tool/linux_amd64/link",
        ):
            path = self.source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture only; source actions are not executed\n")
        self.scalar_data = {}
        for name, relative in cloud_frontends.SCALAR_OUTPUTS:
            path = self.frontend_out / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            data = ("identity:" + name + "\n").encode()
            path.write_bytes(data)
            path.chmod(0o640 if name == "build_number" else 0o644)
            self.scalar_data[name] = (data, stat.S_IMODE(path.stat().st_mode))

    def tearDown(self):
        self.temporary.cleanup()

    def generate(self, existing=()):
        return cloud_frontends.generate(self.source, self.frontend_out, self.destination_out,
                                        self.graph_dir, set(existing))

    def index_companion(self, companion, database):
        graph.index_graph(str(companion), str(self.source), str(database))
        return graph.Graph(database)

    def install_path_alias_files(self, out):
        binary = out / ".path_interposer"
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_bytes(b"fixture frontend binary")
        (out / ".path_interposer_origpath").write_bytes(b"/usr/bin:/bin")
        aliases = out / ".path"
        aliases.mkdir(exist_ok=True)
        (aliases / "expr").symlink_to("../.path_interposer")
        (aliases / "tr").symlink_to("../.path_interposer")

    def test_companion_parses_as_ninja_and_declares_all_missing_frontend_producers(self):
        descriptor, companion = self.generate()
        database = self.root / "frontend-index.sqlite"
        indexed = self.index_companion(companion, database)
        try:
            producers = indexed.producers(descriptor["outputs"], directory_fallback=False)
            self.assertEqual(set(producers), set(descriptor["outputs"]))
            self.assertEqual(len(descriptor["actions"]), 7)
            self.assertEqual(descriptor["native_scalar_overrides"], [])
            self.assertEqual(descriptor["path_interposer"]["producer"], "companion")
            self.assertEqual(descriptor["bpglob"]["producer"], "companion")
            self.assertTrue(all("frontend-out" not in action.get("command", "")
                                for action in descriptor["actions"]))
            self.assertTrue(all("\n" not in action.get("command", "")
                                for action in descriptor["actions"]))
            self.assertTrue(all(str(self.source / "build/soong") in action.get("source_inputs", [])
                                for action in descriptor["actions"]
                                if action["kind"] == "source-go-bootstrap"))
            for name, relative in cloud_frontends.SCALAR_OUTPUTS:
                self.assertEqual((self.destination_out / relative).as_posix(),
                                 next(action["output"] for action in descriptor["actions"]
                                      if action["id"] == name))
        finally:
            indexed.db.close()

    @unittest.skipUnless(shutil.which("ninja"), "Ninja executable is unavailable locally")
    def test_rendered_companion_passes_real_ninja_parse_without_building(self):
        descriptor, companion = self.generate()
        completed = subprocess.run([shutil.which("ninja"), "-f", str(companion), "-t", "targets", "all"],
                                   cwd=self.root, check=True, text=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for output in descriptor["outputs"]:
            self.assertIn(output, completed.stdout)

    def test_existing_scalar_gets_override_not_duplicate_companion_producer(self):
        output = self.destination_out / "soong/build_number.txt"
        descriptor, companion = self.generate(existing=(output,))
        override = descriptor["native_scalar_overrides"][0]
        self.assertEqual(override["name"], "build_number")
        self.assertEqual(override["output"], str(output))
        self.assertEqual(base64.b64decode(override["contents_base64"]), self.scalar_data["build_number"][0])
        self.assertEqual(override["mode"], self.scalar_data["build_number"][1])
        self.assertEqual(override["sha256"], cloud_frontends._sha256(self.scalar_data["build_number"][0]))
        self.assertIn("python3 -c", override["desired_command"])

        indexed = self.index_companion(companion, self.root / "override-index.sqlite")
        try:
            producers = indexed.producers([str(output)], directory_fallback=False)
            self.assertEqual(producers, {})
            other = self.destination_out / "build_date.txt"
            self.assertIn(str(other), indexed.producers([str(other)], directory_fallback=False))
        finally:
            indexed.db.close()

    def test_partial_path_interposer_group_is_rejected_before_output(self):
        present = self.destination_out / ".path_interposer"
        with self.assertRaisesRegex(cloud_frontends.FrontendError, "partial path_interposer"):
            self.generate(existing=(present,))
        self.assertFalse(self.graph_dir.exists())

    def test_physical_symlink_aliases_do_not_complete_partial_producer_group(self):
        self.install_path_alias_files(self.destination_out)
        existing = [self.destination_out / ".path_interposer",
                    self.destination_out / ".path_interposer_origpath"]
        with self.assertRaisesRegex(cloud_frontends.FrontendError, "partial path_interposer"):
            self.generate(existing=existing)
        self.assertFalse(self.graph_dir.exists())

    def test_all_four_logical_path_producers_remain_distinct(self):
        self.install_path_alias_files(self.destination_out)
        logical = [self.destination_out / relative
                   for relative in cloud_frontends.PATH_INTERPOSER_RELATIVE_OUTPUTS]
        descriptor, _ = self.generate(existing=logical)
        existing = descriptor["existing_frontend_outputs"]
        path_outputs = descriptor["path_interposer"]["outputs"]
        self.assertEqual(set(existing) & set(path_outputs), set(path_outputs))
        self.assertEqual(len(set(path_outputs)), 4)
        self.assertEqual(descriptor["path_interposer"]["producer"], "existing")

    def test_physical_alias_files_do_not_change_generated_output_set(self):
        first_out = self.destination_out
        first_graph = self.graph_dir
        partial = [first_out / ".path_interposer", first_out / ".path_interposer_origpath"]
        with self.assertRaisesRegex(cloud_frontends.FrontendError, "partial path_interposer"):
            cloud_frontends.generate(self.source, self.frontend_out, first_out, first_graph, partial)
        second_out = self.root / "destination-with-physical-aliases"
        second_graph = self.root / "graph-with-physical-aliases"
        self.install_path_alias_files(second_out)
        partial = [second_out / ".path_interposer", second_out / ".path_interposer_origpath"]
        with self.assertRaisesRegex(cloud_frontends.FrontendError, "partial path_interposer"):
            cloud_frontends.generate(self.source, self.frontend_out, second_out, second_graph, partial)
        self.assertFalse(first_graph.exists())
        self.assertFalse(second_graph.exists())

    def test_all_existing_helpers_are_not_duplicated(self):
        helper_outputs = [self.destination_out / path
                          for path in cloud_frontends.PATH_INTERPOSER_RELATIVE_OUTPUTS]
        descriptor, companion = self.generate(existing=[
            self.destination_out / "soong/bpglob", *helper_outputs])
        self.assertEqual(descriptor["path_interposer"]["producer"], "existing")
        self.assertEqual(descriptor["bpglob"]["producer"], "existing")
        self.assertEqual(descriptor["actions"], [action for action in descriptor["actions"]
                                                  if action["kind"] == "fresh-frontend-scalar"])
        indexed = self.index_companion(companion, self.root / "existing-index.sqlite")
        try:
            for output in [self.destination_out / "soong/bpglob", *helper_outputs]:
                self.assertEqual(indexed.producers([str(output)], directory_fallback=False), {})
        finally:
            indexed.db.close()

    def test_missing_frontend_scalar_fails_without_defaults_or_graph_files(self):
        missing = self.frontend_out / "build_date.txt"
        missing.unlink()
        with self.assertRaisesRegex(cloud_frontends.FrontendError, "missing frontend scalar"):
            self.generate()
        self.assertFalse(self.graph_dir.exists())

    def test_metadata_and_scalar_writer_commands_preserve_fixture_bytes_and_modes(self):
        descriptor, _ = self.generate()
        path_action = next(action for action in descriptor["actions"] if action["id"] == "path_metadata")
        subprocess.run(["/bin/bash", "-eu", "-c", path_action["command"]], check=True)
        origpath = self.destination_out / ".path_interposer_origpath"
        self.assertEqual(origpath.read_bytes(), b"/usr/bin:/bin")
        self.assertEqual(len(origpath.read_bytes()), 13)
        self.assertEqual(stat.S_IMODE(origpath.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE((self.destination_out / ".path").stat().st_mode), 0o755)
        for name in ("expr", "tr"):
            alias = self.destination_out / ".path" / name
            self.assertTrue(alias.is_symlink())
            self.assertEqual(os.readlink(alias), "../.path_interposer")

        scalar = next(action for action in descriptor["actions"] if action["id"] == "build_number")
        subprocess.run(shlex.split(scalar["command"]), check=True)
        output = Path(scalar["output"])
        self.assertEqual(output.read_bytes(), self.scalar_data["build_number"][0])
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), self.scalar_data["build_number"][1])


if __name__ == "__main__":
    unittest.main()
