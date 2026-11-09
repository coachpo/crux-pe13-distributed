from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import pipeline_report  # noqa: E402

REPOSITORY = "coachpo/crux-pe13-distributed"


class FakeGitHub:
    def __init__(self, runs, issues=()):
        self.runs, self.issues, self.writes = runs, list(issues), []

    def __call__(self, endpoint, method="GET", payload=None):
        if method != "GET":
            self.writes.append((method, endpoint, payload))
            return {"html_url": "https://github.com/" + REPOSITORY + "/issues/9", "number": 9}
        if "/actions/runs/" in endpoint:
            run = endpoint.rsplit("/", 1)[1]
            return {"display_title": self.runs[run], "html_url": "https://run/" + run,
                    "run_attempt": 1, "head_sha": "a" * 40}
        if "/issues?" in endpoint:
            self.assert_bot_filter(endpoint)
            return self.issues
        raise AssertionError(endpoint)

    @staticmethod
    def assert_bot_filter(endpoint):
        if "creator=github-actions%5Bbot%5D" not in endpoint or "state=open" not in endpoint:
            raise AssertionError(endpoint)


class PipelineReportTests(unittest.TestCase):
    def test_release_failure_names_the_build_through_its_assembly_and_compile_runs(self):
        github = FakeGitHub({"43000000003": "ZIP release release (next version) from assembly 43000000002",
                             "43000000002": "Verify fresh PE13 compilation 43000000001",
                             "43000000001": "Fresh PE13 compile 2026110950"})
        with mock.patch.object(pipeline_report, "api", github):
            pipeline_report.report(REPOSITORY, "43000000003", "release")
        method, endpoint, payload = github.writes[0]
        self.assertEqual((method, endpoint), ("POST", f"repos/{REPOSITORY}/issues"))
        self.assertEqual(payload["title"], "PE13 cloud build 2026110950 failed")
        self.assertEqual(payload["assignees"], ["coachpo"])
        self.assertIn("**release** failed", payload["body"])

    def test_later_failure_of_the_same_build_comments_on_its_open_issue(self):
        github = FakeGitHub({"43000000004": "PE13 hosted full 2026110950 from 43000000000"},
                            [{"number": 9, "title": "PE13 cloud build 2026110950 failed",
                              "html_url": "https://issue/9"}])
        with mock.patch.object(pipeline_report, "api", github):
            self.assertEqual(pipeline_report.report(REPOSITORY, "43000000004", "prepare"), "https://issue/9")
        self.assertEqual([write[:2] for write in github.writes],
                         [("POST", f"repos/{REPOSITORY}/issues/9/comments")])

    def test_run_without_a_build_reports_its_stage(self):
        github = FakeGitHub({"43000000005": "Nightly PE13 change check"},
                            [{"number": 3, "title": "PE13 cloud build 2026110950 failed",
                              "pull_request": {}}])
        with mock.patch.object(pipeline_report, "api", github):
            pipeline_report.report(REPOSITORY, "43000000005", "nightly", detail="pin update failed")
        payload = github.writes[0][2]
        self.assertEqual(payload["title"], "PE13 nightly failed")
        self.assertIn("pin update failed", payload["body"])


if __name__ == "__main__":
    unittest.main()
