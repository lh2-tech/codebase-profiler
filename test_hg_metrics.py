#!/usr/bin/env python3
"""Mercurial history metrics: stats, per-changeset churn, merged PRs, no-working-tree repos."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import extract_org_raw_data as ex
import hg_metrics as hg


def _hg(repo: Path, *args: str, user: str = "Ana Muller <ana@example.com>") -> None:
    env = {**os.environ, "HGPLAIN": "1", "HGUSER": user, "HGRCPATH": ""}
    subprocess.run(["hg", *args], cwd=repo, env=env, check=True, capture_output=True)


def build_repo(root: Path) -> Path:
    repo = root / "demo"
    repo.mkdir()
    _hg(repo, "init")
    (repo / "a.py").write_text("print(1)\nprint(2)\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_a.py").write_text("def test_a(): pass\n")
    _hg(repo, "add")
    _hg(repo, "commit", "-m", "initial", "-d", "2020-01-01 10:00 +0000")
    (repo / "a.py").write_text("print(1)\nprint(2)\nprint(3)\n")
    _hg(repo, "commit", "-m", "more (#12)", "-d", "2020-02-01 10:00 +0000")
    _hg(repo, "branch", "feat")
    (repo / "b.txt").write_text("x\n")
    (repo / "bin.dat").write_bytes(bytes(range(256)))
    (repo / "empty.txt").write_text("")
    _hg(repo, "add")
    _hg(repo, "commit", "-m", "feature", "-d", "2020-03-01 10:00 +0000",
        user="dependabot[bot] <bot@github.com>")
    _hg(repo, "update", "default")
    with (repo / "a.py").open("a") as fh:
        fh.write("-- dashes\n")  # a deletion-looking line must still count as an addition
    _hg(repo, "commit", "-m", "main work", "-d", "2020-03-05 10:00 +0000")
    _hg(repo, "merge", "feat")
    _hg(repo, "commit", "-m", "Merged in feat (pull request #7)", "-d", "2020-03-10 10:00 +0000")
    return repo


@unittest.skipUnless(hg.hg_available(), "hg not installed")
class HgMetricsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name)
        cls.repo = build_repo(cls.root)
        cls.stats = hg.aggregate_hg_stats(cls.repo, ex.is_bot_author)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_totals_cover_all_branches(self) -> None:
        self.assertEqual(self.stats["total_commits"], 5)
        self.assertEqual(self.stats["merge_commits"], 1)
        self.assertEqual(self.stats["commits_per_branch"], {"default": 4, "feat": 1})
        self.assertEqual(self.stats["branch_count"], 2)
        self.assertTrue(self.stats["first_commit"].startswith("2020-01-01"))
        self.assertTrue(self.stats["last_commit"].startswith("2020-03-10"))
        self.assertEqual(self.stats["span_days"], 69)

    def test_authors_and_bots(self) -> None:
        self.assertEqual(self.stats["human_authors"], 1)
        self.assertEqual(self.stats["bot_authors"], 1)
        self.assertEqual(self.stats["bot_commit_ratio"], 0.2)
        self.assertEqual(self.stats["authors"][0]["email"], "ana@example.com")

    def test_commit_churn_matches_numstat_semantics(self) -> None:
        out = self.root / "commits.jsonl"
        n = hg.export_commits_jsonl(self.repo, out, is_bot=ex.is_bot_author)
        self.assertEqual(n, 5)
        recs = {r["subject"]: r for r in map(json.loads, out.read_text().splitlines())}
        self.assertEqual(recs["initial"]["additions"], 3)
        self.assertEqual(recs["main work"]["additions"], 1)
        self.assertEqual(recs["main work"]["deletions"], 0)
        self.assertEqual(recs["Merged in feat (pull request #7)"]["files"], [])
        feat = {f["path"]: f for f in recs["feature"]["files"]}
        self.assertEqual(feat["b.txt"]["additions"], 1)
        self.assertEqual(feat["bin.dat"]["additions"], 0)  # binary: no line counts
        self.assertIn("empty.txt", feat)
        self.assertTrue(recs["feature"]["is_bot"])

    def test_detail_cap_keeps_totals(self) -> None:
        out = self.root / "capped" / "commits.jsonl"
        hg.export_commits_jsonl(
            self.repo, out, is_bot=ex.is_bot_author, total_commits=5, max_commits=2,
            write_json=ex.write_json,
        )
        self.assertEqual(len(out.read_text().splitlines()), 2)
        self.assertTrue((out.parent / "commits_detail_meta.json").is_file())

    def test_merged_prs(self) -> None:
        prs = ex.detect_merged_prs_from_hg(self.repo)
        numbers = sorted(p["pr_number"] for p in prs)
        self.assertEqual(numbers, ["12", "7"])

    def test_clone_without_working_tree_is_archived(self) -> None:
        bare = self.root / "bare"
        _hg(self.root, "clone", "-U", str(self.repo), str(bare))
        self.assertTrue(hg.working_copy_is_empty(bare))
        tree, branch, scratch = hg.prepare_working_tree(bare, self.root / "arch")
        self.assertTrue(scratch)
        self.assertEqual(branch, "default")
        self.assertTrue((tree / "a.py").is_file())
        self.assertFalse((tree / ".hg").exists())
        self.assertFalse((tree / ".hg_archival.txt").exists())

    def test_discovery_finds_only_hg(self) -> None:
        (self.root / "gitonly" / ".git").mkdir(parents=True, exist_ok=True)
        found = ex.discover_local_repositories(self.root, "hg")
        self.assertIn("demo", [t.full_name.split("/")[-1] for t in found])
        self.assertNotIn("gitonly", [t.full_name for t in found])
        self.assertTrue(all(t.vcs == "hg" for t in found))
        # vcs survives resume serialisation
        again = ex.load_targets_from_repos_json(self._dump(found))
        self.assertTrue(all(t.vcs == "hg" for t in again))

    def test_epoch_zero_placeholder_date_is_ignored(self) -> None:
        repo = self.root / "epoch"
        repo.mkdir()
        _hg(repo, "init")
        for i, date in enumerate(["2021-05-01 10:00 +0000", "0 0", "2021-06-01 10:00 +0000"]):
            (repo / "f.txt").write_text(f"{i}\n")
            _hg(repo, "add") if i == 0 else None
            _hg(repo, "commit", "-m", f"c{i}", "-d", date)
        stats = hg.aggregate_hg_stats(repo, ex.is_bot_author)
        self.assertEqual(stats["total_commits"], 3)
        self.assertTrue(stats["first_commit"].startswith("2021-05-01"))
        self.assertTrue(stats["last_commit"].startswith("2021-06-01"))

    def _dump(self, targets: list) -> Path:
        p = self.root / "repos.json"
        p.write_text(json.dumps(ex.serialize_targets(targets)))
        return p


if __name__ == "__main__":
    unittest.main()
