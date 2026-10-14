#!/usr/bin/env python3
"""--local-repos-dir runs: the newest-branch rule applies only when EXTRACT_LOCAL_LATEST_BRANCH is set (Squirrel sets it for its own
throwaway clones); a user's own working copy is never moved otherwise."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import extract_org_raw_data as ex
from test_git_hardening import _git


def _origin_with_newer_branch(root: Path) -> Path:
    origin = root / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    (origin / "a.txt").write_text("1")
    _git(origin, "add", ".")
    _git(origin, "commit", "-qm", "old", date="2020-01-01T00:00:00")
    _git(origin, "checkout", "-qb", "newer")
    (origin / "b.txt").write_text("2")
    _git(origin, "add", ".")
    _git(origin, "commit", "-qm", "new", date="2024-01-01T00:00:00")
    _git(origin, "checkout", "-q", "main")
    return origin


def _head(repo: Path) -> str:
    return ex.run_git(repo, "symbolic-ref", "--short", "HEAD").strip()


class LocalLatestBranchTests(unittest.TestCase):
    def test_a_clone_of_an_export_moves_to_the_newest_branch(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            clone = root / "clone"
            subprocess.run(["git", "clone", "-q", str(_origin_with_newer_branch(root)), str(clone)], check=True, capture_output=True)
            self.assertEqual(_head(clone), "main")
            self.assertEqual(ex.select_latest_branch(clone, "main", None, local=True), "newer")
            self.assertEqual(_head(clone), "newer")

    def test_a_repo_with_no_remote_uses_its_own_branches(self):
        with tempfile.TemporaryDirectory() as d:
            origin = _origin_with_newer_branch(Path(d))       # no clone: only refs/heads/*
            self.assertEqual(ex.select_latest_branch(origin, "main", None, local=True), "newer")
            self.assertEqual(_head(origin), "newer")

    def test_platform_behaviour_is_unchanged_without_local(self):
        with tempfile.TemporaryDirectory() as d:
            origin = _origin_with_newer_branch(Path(d))       # no remote-tracking refs: nothing to select for a platform run
            self.assertEqual(ex.select_latest_branch(origin, "main", None), "")
            self.assertEqual(_head(origin), "main")

    def test_a_tie_stays_on_the_default_branch(self):
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / "r"
            repo.mkdir()
            _git(repo, "init", "-q", "-b", "main")
            (repo / "a.txt").write_text("1")
            _git(repo, "add", ".")
            _git(repo, "commit", "-qm", "x", date="2022-01-01T00:00:00")
            _git(repo, "branch", "other")
            self.assertEqual(ex.select_latest_branch(repo, "main", None, local=True), "main")

    def _map(self, root: Path, data) -> None:
        import json
        (root / "map.json").write_text(data if isinstance(data, str) else json.dumps(data))
        os.environ["EXTRACT_LOCAL_BRANCH_MAP"] = str(root / "map.json")
        self.addCleanup(os.environ.pop, "EXTRACT_LOCAL_BRANCH_MAP", None)

    def test_the_client_branch_is_found_by_directory_name_case_insensitive(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._map(root, {"MyRepo": "dev"})
            self.assertEqual(ex.client_branch_for(root / "myrepo"), "dev")
            self.assertEqual(ex.client_branch_for(root / "other"), "")

    def test_a_missing_bad_or_unset_map_names_no_branch(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.assertEqual(ex.client_branch_for(root / "r"), "")
            for bad in ("not json", "[1]", '{"r": 5}'):
                self._map(root, bad)
                self.assertEqual(ex.client_branch_for(root / "r"), "", bad)
            os.environ["EXTRACT_LOCAL_BRANCH_MAP"] = str(root / "missing.json")
            self.assertEqual(ex.client_branch_for(root / "r"), "")

    def test_checkout_client_branch_uses_own_branch_then_origin(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            origin = _origin_with_newer_branch(root)          # main (old), newer
            self.assertEqual(ex.checkout_client_branch(origin, "main"), "main")
            self.assertEqual(_head(origin), "main")
            clone = root / "clone"
            subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True, capture_output=True)
            self.assertEqual(ex.checkout_client_branch(clone, "newer"), "newer")   # only refs/remotes/origin/newer
            self.assertEqual(_head(clone), "newer")

    def test_checkout_client_branch_with_unknown_branch_leaves_head(self):
        with tempfile.TemporaryDirectory() as d:
            origin = _origin_with_newer_branch(Path(d))
            self.assertEqual(ex.checkout_client_branch(origin, "nope"), "")
            self.assertEqual(_head(origin), "main")

    def test_the_setting_is_off_by_default(self):
        self.assertFalse(ex.LOCAL_LATEST_BRANCH)


if __name__ == "__main__":
    unittest.main()
