#!/usr/bin/env python3
"""for-check.csv: snapshots never raise, rows are long-format, summary.xlsx gets a second sheet."""

from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import for_check as fc


class _Proc:
    def __init__(self, code=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = code, out, err


class LsRemoteTests(unittest.TestCase):
    def test_keeps_heads_tags_and_pr_refs_only_and_token_not_in_argv(self):
        seen = {}

        def fake_run(cmd, **kw):
            seen["cmd"], seen["env"] = cmd, kw["env"]
            return _Proc(out="a1\trefs/heads/main\nb2\trefs/tags/v1\nc3\trefs/pull/7/head\nd4\trefs/notes/x\n")

        refs = fc.ls_remote_refs("https://h/o/r.git", "x-access-token", "SECRET", run=fake_run)
        self.assertEqual(refs, {"refs/heads/main": "a1", "refs/tags/v1": "b2", "refs/pull/7/head": "c3"})
        self.assertNotIn("SECRET", " ".join(seen["cmd"]))
        self.assertIn("http.extraHeader", seen["env"]["GIT_CONFIG_KEY_0"])

    def test_failure_redacts_token(self):
        with self.assertRaises(RuntimeError) as ctx:
            fc.ls_remote_refs("u", "x", "SECRET", run=lambda *a, **k: _Proc(2, err="bad SECRET here"))
        self.assertNotIn("SECRET", str(ctx.exception))


class SnapshotTests(unittest.TestCase):
    def test_gitlab_counts_use_x_total_and_blank_on_failure(self):
        def fake_get(url, headers, **kw):
            if "statistics=true" in url:
                return {"statistics": {"commit_count": 9}, "default_branch": "main", "wiki_enabled": True}, {}
            if "/labels" in url:
                return [], {"X-Total": "5"}
            raise RuntimeError("HTTP 403")

        with mock.patch.object(fc, "http_get_json", fake_get):
            c = fc.gitlab_counts("g/p", token="t", host="gitlab.com")
        self.assertEqual((c["commits"], c["labels"], c["has_wiki"]), (9, 5, "true"))
        self.assertEqual(c["collaborators"], "")  # unknown stays blank, never a guess
        self.assertTrue(any(e.startswith("collaborators:") for e in c["_errors"]))

    def test_github_count_from_link_header(self):
        link = '<https://api.github.com/x?per_page=1&page=294>; rel="last"'
        with mock.patch.object(fc, "http_get_json", lambda *a, **k: ([{}], {"Link": link})):
            self.assertEqual(fc._count("https://api.github.com/x", {}), 294)

    def test_snapshot_never_raises(self):
        with mock.patch.object(fc, "github_counts", side_effect=RuntimeError("boom")), \
                mock.patch.object(fc, "ls_remote_refs", side_effect=RuntimeError("nope")):
            snap = fc.snapshot("github", "o/r", token="t", host="github.com")
        self.assertEqual(snap["counts"], {})
        self.assertEqual(len(snap["errors"]), 2)


class RunCsvTests(unittest.TestCase):
    def _run_dir(self, tmp: Path) -> Path:
        api = tmp / "api" / "o__r"
        git = tmp / "git" / "o__r"
        api.mkdir(parents=True)
        git.mkdir(parents=True)
        (api / "repo.json").write_text("{}")
        (git / "git_stats.json").write_text("{}")
        before = {"counts": {"prs": 5, "labels": ""}, "refs": {"refs/heads/main": "a1"}, "errors": ["x: y"]}
        after = {"counts": {"prs": 5}, "refs": {"refs/heads/main": "a1"}, "errors": []}
        fc.save_repo(api, platform="github", host="github.com", started_at="t0", before=before, after=after,
                     captured={"repo": "o/r", "merged_prs": 4})
        return tmp

    def test_rows_and_csv(self):
        with tempfile.TemporaryDirectory() as d:
            run = self._run_dir(Path(d))
            path = fc.write_run_csv(run)
            rows = list(csv.DictReader(path.open()))
        got = {(r["repo"], r["section"], r["key"]): r["value"] for r in rows}
        self.assertEqual(got[("o/r", "counts_before", "prs")], "5")
        self.assertEqual(got[("o/r", "counts_before", "labels")], "")
        self.assertEqual(got[("o/r", "ref_after", "refs/heads/main")], "a1")
        self.assertEqual(got[("o/r", "captured", "merged_prs")], "4")
        self.assertIn(("o/r", "file", "api/o__r/repo.json"), got)
        self.assertNotIn(("o/r", "file", "api/o__r/for_check.json"), got)
        self.assertEqual(got[("o/r", "error", "before")], "x: y")

    def test_workbook_has_second_sheet(self):
        try:
            import openpyxl
        except ImportError:
            self.skipTest("openpyxl not installed")
        with tempfile.TemporaryDirectory() as d:
            run = self._run_dir(Path(d))
            (run / "summary.csv").write_text("org,repo\no,r\n")
            fc.write_run_csv(run)
            xlsx = fc.write_workbook(run / "summary.csv")
            wb = openpyxl.load_workbook(xlsx)
        self.assertEqual(wb.sheetnames, ["summary", "for-check"])
        self.assertEqual([c.value for c in wb["for-check"][1]], fc.COLUMNS)

    def test_workbook_without_for_check_has_one_sheet(self):
        try:
            import openpyxl
        except ImportError:
            self.skipTest("openpyxl not installed")
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "summary.csv").write_text("org,repo\no,r\n")
            wb = openpyxl.load_workbook(fc.write_workbook(Path(d) / "summary.csv"))
        self.assertEqual(wb.sheetnames, ["summary"])


class LocalTargetTests(unittest.TestCase):
    def test_local_target_is_unavailable_not_an_error(self):
        import extract_org_raw_data as ex

        target = ex.RepoTarget(platform="local", org="o", full_name="o/r", meta={}, local_path=Path("."))
        snap = ex._for_check_snapshot(target, {}, "g", "l", "b", None, "github.com", "gitlab.com")
        self.assertEqual(snap["counts"], {})
        self.assertIn("no platform source", snap["errors"][0])


if __name__ == "__main__":
    unittest.main()
