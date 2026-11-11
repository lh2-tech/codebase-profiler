#!/usr/bin/env python3
"""Tamper evidence: per-repo facts, run manifest, HMAC, challenge, and detection of edited / added files."""

from __future__ import annotations

import csv
import gzip
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import integrity as ig
from test_git_hardening import _git


def _make_run(tmp: Path) -> Path:
    repo = tmp / "src"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    for i in range(3):
        (repo / "f.txt").write_text(f"v{i}\n")
        _git(repo, "add", ".")
        _git(repo, "commit", "-q", "-m", f"c{i}")
    run = tmp / "run"
    gdir = run / "git" / "r"
    gdir.mkdir(parents=True)
    hashes = ig._git(repo, "rev-list", "--reverse", "HEAD")[1].split()
    with gzip.open(gdir / "commit_hashes.txt.gz", "wt") as fh:
        fh.write("\n".join(sorted(hashes)) + "\n")
    (gdir / "scc.json").write_text(json.dumps({"loc": 3, "by_language": {"Text": 3}}))
    (gdir / "git_stats.json").write_text(json.dumps(
        {"total_commits": 3, "first_commit": "a", "last_commit": "b", "span_days": 0,
         "authors": [{"commits": 3}]}))
    (gdir / "commits.jsonl").write_text("".join(json.dumps({"sha": h}) + "\n" for h in hashes))
    with (run / "summary.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, ["repo", "loc", "total_commits", "first_commit", "last_commit_date", "span_days",
                                "merged_prs"])
        w.writeheader()
        w.writerow({"repo": "r", "loc": 3, "total_commits": 3, "first_commit": "a", "last_commit_date": "b",
                    "span_days": 0, "merged_prs": 0})
    ig.write_repo_integrity(repo, gdir, "main")
    return run


class IntegrityTests(unittest.TestCase):
    def test_facts_match_the_repository(self):
        with tempfile.TemporaryDirectory() as t:
            run = _make_run(Path(t))
            facts = json.loads((run / "git/r/integrity.json").read_text())
            self.assertEqual(facts["commits"], 3)
            self.assertEqual(facts["merge_commits"], 0)
            self.assertTrue(facts["fsck_ok"])
            self.assertEqual(len(facts["head_tree"]), 40)

    def test_clean_run_verifies_and_edits_are_caught(self):
        with tempfile.TemporaryDirectory() as t, mock.patch.dict(os.environ, {ig.KEY_ENV: "k"}):
            run = _make_run(Path(t))
            ig.write_run_integrity(run, challenge="C1")
            self.assertEqual(ig.verify_run(run, key="k", challenge="C1")["problems"], [])
            self.assertTrue(ig.verify_run(run, key="bad", challenge="C1")["problems"])
            self.assertTrue(ig.verify_run(run, key="k", challenge="C2")["problems"])
            (run / "summary.csv").write_text((run / "summary.csv").read_text().replace(",3,3,", ",9,3,"))
            problems = ig.verify_run(run, key="k", challenge="C1")["problems"]
            self.assertTrue(any("modified file: summary.csv" in p for p in problems))
            self.assertTrue(any("summary loc" in p for p in problems))

    def test_added_and_missing_files_are_caught(self):
        with tempfile.TemporaryDirectory() as t:
            run = _make_run(Path(t))
            ig.write_run_integrity(run)
            (run / "git/extra.txt").write_text("x")
            (run / "git/r/scc.json").unlink()
            problems = ig.verify_run(run)["problems"]
            self.assertTrue(any("added later" in p for p in problems))
            self.assertTrue(any("missing file" in p for p in problems))

    def test_run_without_manifest_is_reported(self):
        with tempfile.TemporaryDirectory() as t:
            run = _make_run(Path(t))
            self.assertTrue(any("INTEGRITY.json is missing" in p for p in ig.verify_run(run)["problems"]))


if __name__ == "__main__":
    unittest.main()
