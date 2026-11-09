import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import pipeline_code


class PipelineCodeTests(unittest.TestCase):
    def test_same_code_preparation_inputs_are_accepted_once(self):
        with mock.patch.object(pipeline_code, "api", return_value={"head_sha": "a" * 40}) as api:
            self.assertEqual(pipeline_code.verify("owner/repo", "a" * 40, ["12", "", "12", "34"]),
                             ["12", "34"])
        self.assertEqual(api.call_args_list,
                         [mock.call("repos/owner/repo/actions/runs/12"),
                          mock.call("repos/owner/repo/actions/runs/34")])

    def test_moving_main_refuses_stage_input_before_admission(self):
        with mock.patch.object(pipeline_code, "api", return_value={"head_sha": "b" * 40}):
            with self.assertRaisesRegex(pipeline_code.PipelineCodeError, "Start a full freeze"):
                pipeline_code.verify("owner/repo", "a" * 40, ["12"])

    def test_missing_upstream_identity_is_not_accepted(self):
        with mock.patch.object(pipeline_code, "api", return_value={}):
            with self.assertRaises(pipeline_code.PipelineCodeError):
                pipeline_code.verify("owner/repo", "a" * 40, ["12"])

    def test_invalid_identity_or_run_makes_no_api_request(self):
        for sha, runs in [("main", ["12"]), ("a" * 40, [""]), ("a" * 40, ["12", "bad"])]:
            with self.subTest(sha=sha, runs=runs), mock.patch.object(pipeline_code, "api") as api:
                with self.assertRaises(pipeline_code.PipelineCodeError):
                    pipeline_code.verify("owner/repo", sha, runs)
                api.assert_not_called()

    def test_api_reads_the_run_using_gh_without_mutating_it(self):
        response = subprocess.CompletedProcess([], 0, json.dumps({"head_sha": "a" * 40}), "")
        with mock.patch.object(pipeline_code.subprocess, "run", return_value=response) as run:
            self.assertEqual(pipeline_code.api("repos/owner/repo/actions/runs/12"), {"head_sha": "a" * 40})
        run.assert_called_once_with(["gh", "api", "repos/owner/repo/actions/runs/12"],
                                    capture_output=True, text=True, check=True)


if __name__ == "__main__":
    unittest.main()
