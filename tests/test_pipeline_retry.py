import json
import copy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import pipeline_retry
import cloud_reuse


SHA = "a" * 40
CONTEXT = {
    "schema_version": 1, "execution_id": "123", "worker_commit": SHA,
    "repository": "coachpo/crux-pe13-distributed", "build_number": "2026111350",
    "pack_run": "111", "shard_run": "112",
    "reuse_policy": {"version": 1, "execution_digest": "e" * 64, "force_cold": False},
    "tasks": {"w000-s001": {}, "w001-s001": {}},
}
EVIDENCE = {
    "schema_version": 1, "status": "failed", "failure_class": "content-validation",
    "stage": "assemble", "execution_id": "123", "worker_commit": SHA,
    "build_number": "2026111350", "reused_tasks": ["w000-s001"], "error": "fixture validation",
}


class PipelineRetryTests(unittest.TestCase):
    def retry(self, *, context=None, evidence=None, stage="assemble", ref="refs/heads/main",
              sha=SHA, main_sha=SHA, runs=None):
        runner = Mock(return_value=type("Result", (), {"stdout": ""})())
        result = pipeline_retry.retry(context or CONTEXT, evidence or EVIDENCE, stage, "123",
                                      CONTEXT["repository"], ref, sha, run=runner,
                                      runs=[] if runs is None else runs, main_sha=main_sha)
        return result, runner

    def test_dispatch_preserves_admitted_pins_and_marks_one_cold_retry(self):
        result, runner = self.retry()
        self.assertEqual(result["result"], "dispatched")
        self.assertEqual(runner.call_args.args[0], [
            "gh", "workflow", "run", "build-standard.yml", "--repo", CONTEXT["repository"],
            "--ref", "main", "-f", "pack_run=111", "-f", "shard_run=112",
            "-f", "build_number=2026111350", "-f", "force_cold=true", "-f", "cold_retry_of=123"])

    def test_duplicate_retry_run_is_idempotent(self):
        result, runner = self.retry(runs=[{"displayTitle": "Fresh PE13 compile 2026111350 cold retry of 123",
                                          "headSha": SHA}])
        self.assertEqual(result["result"], "already-dispatched")
        runner.assert_not_called()

    def test_rejects_main_moved_after_origin_execution(self):
        runner = Mock(return_value=type("Result", (), {"stdout": ""})())
        with self.assertRaisesRegex(pipeline_retry.RetryError, "main has moved"):
            pipeline_retry.retry(CONTEXT, EVIDENCE, "assemble", "123", CONTEXT["repository"],
                                 "refs/heads/main", SHA, run=runner, runs=[], main_sha="b" * 40)
        runner.assert_not_called()

    def test_rejects_untyped_network_or_publication_failure(self):
        for change in ({"failure_class": "transport"}, {"stage": "release"}, {"reused_tasks": []}):
            with self.subTest(change=change), self.assertRaises(pipeline_retry.RetryError):
                self.retry(evidence={**EVIDENCE, **change})

    def test_rejects_cold_or_already_retried_execution(self):
        for policy in ({"force_cold": True, "cold_retry_of": ""},
                       {"force_cold": False, "cold_retry_of": "121"}):
            with self.subTest(policy=policy), self.assertRaises(pipeline_retry.RetryError):
                self.retry(context={**CONTEXT, "reuse_policy": policy})

    def test_rejects_context_from_non_main_or_mismatched_code(self):
        with self.assertRaisesRegex(pipeline_retry.RetryError, "main execution commit"):
            self.retry(ref="refs/heads/feature")
        with self.assertRaisesRegex(pipeline_retry.RetryError, "does not match"):
            self.retry(sha="b" * 40)

    def test_compile_receipts_need_typed_content_failure_and_reused_dependency(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            context = copy.deepcopy(CONTEXT)
            for task, dependencies in (("w000-s001", []), ("w001-s001", ["w000-s001"])):
                context["tasks"][task].update(id=task, depends_on=dependencies,
                                               input_identity=("1" if task == "w000-s001" else "2") * 64,
                                               extra_exports=[], local_runtime_targets=[])
            dependency = root / "fresh-receipt-w000-s001"
            dependency.mkdir()
            spec = {"path": "/out/value", "type": "file", "mode": 0o644, "size": 1, "sha256": "d" * 64}
            native = {"status": "success", "id": "w000-s001", "run_id": "122",
                      "worker_commit": "c" * 40, "outputs": [spec],
                      "build_environment": {"CCACHE_DISABLE": "1"}}
            basis = {"schema_version": 1, "version": 1,
                     "execution_digest": context["reuse_policy"]["execution_digest"],
                     "input_identity": context["tasks"]["w000-s001"]["input_identity"],
                     "runtime": {"CCACHE_DISABLE": "1"}, "dependencies": [],
                     "additional_exports": [], "prebuild_targets": [], "kernel_source_alias": None}
            origin = {"repository": context["repository"], "run_id": "122", "worker_commit": "c" * 40,
                      "task_id": "w000-s001", "receipt_sha256": cloud_reuse.digest(native),
                      "artifact_id": 456, "artifact_digest": "sha256:" + "f" * 64}
            envelope = {"schema_version": 1, "mode": "reused", "key": cloud_reuse.content_key(basis),
                        "basis": basis, "native_receipt": native, "origin": origin}
            (dependency / "receipt.json").write_text(json.dumps({"status": "success", "id": "w000-s001",
                "reuse": envelope, "outputs": [spec]}))
            failure = root / "fresh-receipt-w001-s001"
            failure.mkdir()
            (failure / "receipt.json").write_text(json.dumps({
                **EVIDENCE, "stage": "compile", "id": "w001-s001", "status": "failed"}))
            evidence = pipeline_retry.compile_evidence(root, context, "123")
            self.assertEqual(evidence["reused_tasks"], ["w000-s001"])
            pipeline_retry.validate_retry(context, evidence, "compile", "123", context["repository"],
                                          "refs/heads/main", SHA)
            (failure / "receipt.json").write_text(json.dumps({
                **EVIDENCE, "stage": "compile", "id": "w001-s001", "status": "failed",
                "reused_tasks": []}))
            with self.assertRaisesRegex(pipeline_retry.RetryError, "actual reused inputs"):
                pipeline_retry.compile_evidence(root, context, "123")


if __name__ == "__main__":
    unittest.main()
