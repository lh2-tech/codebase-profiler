#!/usr/bin/env python3
"""Extended final clone try, end-of-run retries, subgroup row identity, and the UI controls that went away."""

from __future__ import annotations

import csv
import logging
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import extract_org_raw_data as ex

LOG = logging.getLogger("test-clone-retry")


def _timeout(*_args, **kwargs):
    raise subprocess.TimeoutExpired(cmd="git", timeout=kwargs["timeout"])


def _ok(*_args, **_kwargs):
    return SimpleNamespace(returncode=0, stdout="", stderr="")


def _fail(text):
    return lambda *_a, **_k: SimpleNamespace(returncode=128, stdout="", stderr=text)


class ExtendedCloneTests(unittest.TestCase):
    def _clone(self, run, **kwargs):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(ex.subprocess, "run", side_effect=run) as proc, \
                mock.patch.object(ex.time, "sleep"):
            try:
                ex.clone_repo("https://x/y.git", Path(tmp) / "dest", log=LOG, **kwargs)
                error = None
            except RuntimeError as exc:
                error = exc
        return proc, error

    def test_a_third_timeout_is_followed_by_one_extended_try_then_it_ends(self):
        proc, error = self._clone(_timeout, timeout=30, retries=2, extended_timeout=90)
        self.assertEqual([c.kwargs["timeout"] for c in proc.call_args_list], [30, 30, 30, 90])
        self.assertIn("timed out after 90s", str(error))

    def test_the_extended_try_can_succeed(self):
        calls = []

        def run(*args, **kwargs):
            calls.append(kwargs["timeout"])
            return _timeout(*args, **kwargs) if kwargs["timeout"] < 90 else _ok()

        proc, error = self._clone(run, timeout=30, retries=2, extended_timeout=90)
        self.assertIsNone(error)
        self.assertEqual(calls, [30, 30, 30, 90])

    def test_a_network_failure_also_gets_the_extended_try(self):
        proc, error = self._clone(_fail("fatal: Could not resolve host"), timeout=30, retries=1, extended_timeout=90)
        self.assertEqual([c.kwargs["timeout"] for c in proc.call_args_list], [30, 30, 90])
        self.assertIsNotNone(error)

    def test_auth_and_not_found_end_at_once(self):
        for text in ("remote: HTTP Basic: Access denied 403", "fatal: repository not found 404"):
            proc, error = self._clone(_fail(text), timeout=30, retries=2, extended_timeout=90)
            self.assertEqual(proc.call_count, 1, text)
            self.assertIsNotNone(error)

    def test_an_unclassified_failure_gets_no_extended_try(self):
        proc, error = self._clone(_fail("error: unable to create file a.js: File exists"), timeout=30, retries=2,
                                  extended_timeout=90)
        self.assertEqual(proc.call_count, 3)
        self.assertIsNotNone(error)

    def test_extended_timeout_zero_turns_it_off(self):
        proc, _ = self._clone(_timeout, timeout=30, retries=2, extended_timeout=0)
        self.assertEqual(proc.call_count, 3)


def _target(platform, full_name, meta=None):
    return ex.RepoTarget(platform=platform, org=full_name.split("/")[0], full_name=full_name, meta=meta or {})


class SubgroupTests(unittest.TestCase):
    def test_subgroup_comes_from_the_gitlab_path_and_the_bitbucket_project(self):
        self.assertEqual(ex.target_subgroup(_target("gitlab", "ft/mobile-apps/FTAndroid")), "mobile-apps")
        self.assertEqual(ex.target_subgroup(_target("gitlab", "ft/a/b/repo")), "a/b")
        self.assertEqual(ex.target_subgroup(_target("gitlab", "ft/repo")), "")
        self.assertEqual(ex.target_subgroup(_target("bitbucket", "ws/repo", {"project": {"name": "Payments", "key": "PAY"}})), "Payments")
        self.assertEqual(ex.target_subgroup(_target("bitbucket", "ws/repo", {"project": {"key": "PAY"}})), "PAY")
        self.assertEqual(ex.target_subgroup(_target("bitbucket", "ws/repo", {})), "")
        self.assertEqual(ex.target_subgroup(_target("github", "org/repo")), "")

    def test_the_column_sits_right_after_org_and_dedup_stays_last(self):
        self.assertEqual(ex.SUMMARY_FIELDS[:2], ["org", "subgroup"])
        self.assertEqual(ex.SUMMARY_FIELDS[-3:], ["duplicate_pct", "duplicate_commits", "duplicates_with"])

    def test_same_named_repos_in_two_subgroups_keep_separate_rows(self):
        a, b = _target("gitlab", "ft/platforms/ocr-service"), _target("gitlab", "ft/invoicing/ocr-service")
        self.assertNotEqual(ex.target_row_key(a), ex.target_row_key(b))
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            job = ex.JobCheckpoint.load_or_create(run_dir, total=2)
            for target, loc in ((a, 1), (b, 2)):
                row = ex.empty_summary_row(target.org, "ocr-service", ex.target_subgroup(target))
                row["loc"] = loc
                job.record(row)
            self.assertEqual([(r["subgroup"], r["loc"]) for r in job.rows()], [("invoicing", 2), ("platforms", 1)])
            job.replace_rows(job.rows())
            with job.summary_path.open(newline="", encoding="utf-8") as fh:
                self.assertEqual([r["subgroup"] for r in csv.DictReader(fh)], ["invoicing", "platforms"])
            reloaded = ex.JobCheckpoint.load_or_create(run_dir, total=2)
            self.assertEqual(reloaded.completed_ok_keys(), {ex.target_row_key(a), ex.target_row_key(b)})

    def test_resume_skips_only_the_subgroup_that_finished(self):
        a, b = _target("gitlab", "ft/platforms/ocr-service"), _target("gitlab", "ft/invoicing/ocr-service")
        with tempfile.TemporaryDirectory() as tmp:
            job = ex.JobCheckpoint.load_or_create(Path(tmp), total=2)
            job.record(ex.empty_summary_row(a.org, "ocr-service", "platforms"))
            todo, skipped_ok, _ = ex.filter_targets_for_resume([a, b], job, retry_failed=False, retry_all_failed=False)
            self.assertEqual(todo, [b])
            self.assertEqual(skipped_ok, 1)


class EndOfRunRetryTests(unittest.TestCase):
    def _row(self, target, error="", error_class=""):
        row = ex.empty_summary_row(target.org, target.full_name.split("/")[-1], ex.target_subgroup(target))
        row["error"], row["error_class"] = error, error_class
        return row

    def _run(self, outcomes, retry_passes=1):
        """outcomes: {full_name: [row error classes per attempt]}; '' means success."""
        targets = [_target("gitlab", name) for name in outcomes]
        attempts = {name: 0 for name in outcomes}

        def process(target, **_kwargs):
            seq = outcomes[target.full_name]
            error_class = seq[min(attempts[target.full_name], len(seq) - 1)]
            attempts[target.full_name] += 1
            return self._row(target, error=f"{error_class} problem" if error_class else "", error_class=error_class)

        with tempfile.TemporaryDirectory() as tmp:
            job = ex.JobCheckpoint.load_or_create(Path(tmp), total=len(targets))
            with mock.patch.object(ex, "process_repo", side_effect=process):
                ex.run_passes(targets, job, workers=2, retry_passes=retry_passes, log=LOG, process_kwargs={})
            statuses = {k: v["status"] for k, v in job.status_by_key.items()}
        return attempts, statuses

    def test_a_timeout_is_retried_at_the_end_and_the_row_is_replaced(self):
        attempts, statuses = self._run({"ft/g/slow": ["timeout", ""], "ft/g/fine": [""]})
        self.assertEqual(attempts, {"ft/g/slow": 2, "ft/g/fine": 1})
        self.assertEqual(set(statuses.values()), {"ok"})

    def test_not_found_auth_and_unknown_are_not_retried(self):
        attempts, statuses = self._run({"ft/g/gone": ["not_found"], "ft/g/denied": ["auth"], "ft/g/odd": ["unknown"]})
        self.assertEqual(attempts, {"ft/g/gone": 1, "ft/g/denied": 1, "ft/g/odd": 1})
        self.assertEqual(set(statuses.values()), {"failed"})

    def test_a_repo_that_keeps_timing_out_is_retried_once_and_then_left_failed(self):
        attempts, statuses = self._run({"ft/g/slow": ["timeout"]})
        self.assertEqual(attempts["ft/g/slow"], 2)
        self.assertEqual(statuses, {"ft/g/slow": "failed"})

    def test_zero_passes_disables_the_retry(self):
        attempts, _ = self._run({"ft/g/slow": ["timeout", ""]}, retry_passes=0)
        self.assertEqual(attempts["ft/g/slow"], 1)

    def test_failed_retryable_repos_from_an_earlier_pass_of_the_run_are_picked_up(self):
        a = _target("gitlab", "ft/g/slow")
        with tempfile.TemporaryDirectory() as tmp:
            job = ex.JobCheckpoint.load_or_create(Path(tmp), total=1)
            job.record(self._row(a, "timeout problem", "timeout"))
            self.assertEqual(ex.retryable_failed_targets([a], job), [a])
            job.record(self._row(a, "nope", "not_found"))
            self.assertEqual(ex.retryable_failed_targets([a], job), [])


class UiControlsTests(unittest.TestCase):
    def test_no_retry_or_duplicate_buttons_and_no_flags_for_them(self):
        import extract_ui

        page = extract_ui.page()
        self.assertNotIn('name="retry_failed"', page)
        self.assertNotIn('name="compare_commits"', page)
        self.assertNotIn("Retry failed repositories", page)
        settings = extract_ui.extract_form_settings({})
        self.assertNotIn("retry_failed", settings)
        self.assertNotIn("compare_commits", settings)
        self.assertIn("Resume previous run", page)


if __name__ == "__main__":
    unittest.main()
