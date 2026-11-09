#!/usr/bin/env python3
"""merged_prs is the hosting platform's PR count; merge commits found in the history are reported apart, as local_merges.

An offline run never asks the platform, so merged_prs is blank there and the merge/squash commits go to local_merges: a merge
commit (a local `git merge` or `git pull` makes one) is not a pull request, and the two must never share a column.
"""

from __future__ import annotations

import csv
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import extract_org_raw_data as ex
from test_git_hardening import _git

HERE = Path(__file__).resolve().parent


def _repo_with_one_merge(path: Path) -> None:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    (path / "a.py").write_text("print(1)\n")
    _git(path, "add", ".")
    _git(path, "commit", "-qm", "first", date="2024-01-01T00:00:00")
    _git(path, "checkout", "-qb", "feature")
    (path / "b.py").write_text("print(2)\n")
    _git(path, "add", ".")
    _git(path, "commit", "-qm", "feature work", date="2024-01-02T00:00:00")
    _git(path, "checkout", "-q", "main")
    (path / "c.py").write_text("print(3)\n")
    _git(path, "add", ".")
    _git(path, "commit", "-qm", "main work", date="2024-01-03T00:00:00")
    _git(path, "merge", "-q", "--no-ff", "feature", "-m", "Merge branch 'feature'", date="2024-01-04T00:00:00")


class LocalMergesTests(unittest.TestCase):
    def test_local_merges_sits_right_after_merged_prs(self):
        i = ex.SUMMARY_FIELDS.index("merged_prs")
        self.assertEqual(ex.SUMMARY_FIELDS[i + 1], "local_merges")

    def test_an_offline_run_reports_merges_as_local_merges_and_leaves_merged_prs_blank(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _repo_with_one_merge(root / "repos" / "acme" / "shop")
            out = root / "out"
            proc = subprocess.run(
                [sys.executable, str(HERE / "extract_org_raw_data.py"), "--offline", "--local-repos-dir",
                 str(root / "repos"), "--output-dir", str(out)],
                cwd=HERE, capture_output=True, text=True, timeout=600)
            self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
            summaries = list(out.glob("*/summary.csv"))
            self.assertEqual(len(summaries), 1, list(out.iterdir()))
            with summaries[0].open(newline="", encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row["merged_prs"], "", "the platform was not asked: its PR count is unknown, not the merges")
            self.assertEqual(row["local_merges"], "1", row)
            header = summaries[0].read_text(encoding="utf-8").splitlines()[0].split(",")
            self.assertEqual(header[header.index("merged_prs") + 1], "local_merges")


if __name__ == "__main__":
    unittest.main()
