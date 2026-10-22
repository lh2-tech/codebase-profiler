#!/usr/bin/env python3
"""Commit-hash deduplication: what share of a repo's commits also exist in another repo of the same run.

Pure-function tests use made-up hashes; the git and run-level tests build tiny repos in temp dirs with real git.
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import commit_dedup as cd
import extract_org_raw_data as ex
from test_git_hardening import _git

HEX40 = re.compile(r"^[0-9a-f]{40}$")


def h(n: int) -> str:
    return f"{n:040x}"


def hs(*numbers: int) -> list[str]:
    return sorted(h(n) for n in numbers)


class ComputeTests(unittest.TestCase):
    def test_identical_repos_are_100_percent_both_ways(self):
        result = cd.compute({"a": hs(1, 2, 3), "b": hs(1, 2, 3)})
        for name in ("a", "b"):
            self.assertEqual(result.stats[name].duplicate_pct, 100.0)
            self.assertEqual(result.stats[name].duplicate_commits, 3)
        self.assertEqual(result.pairs, [cd.Pair("a", "b", 3, 100.0, 100.0)])

    def test_a_fork_with_extra_commits_scores_differently_from_its_parent(self):
        result = cd.compute({"parent": hs(1, 2, 3, 4), "fork": hs(1, 2, 3, 4, 5, 6)})
        self.assertEqual(result.stats["parent"].duplicate_pct, 100.0)
        self.assertEqual(result.stats["fork"].duplicate_pct, 66.7)
        self.assertEqual(result.stats["fork"].duplicate_commits, 4)
        self.assertEqual(result.pairs, [cd.Pair("fork", "parent", 4, 66.7, 100.0)])

    def test_unrelated_repos_are_zero_not_blank(self):
        result = cd.compute({"a": hs(1, 2), "b": hs(3, 4)})
        self.assertEqual(result.stats["a"].duplicate_pct, 0.0)
        self.assertEqual(result.stats["a"].duplicate_commits, 0)
        self.assertEqual(result.stats["a"].top, [])
        self.assertEqual(result.pairs, [])

    def test_a_single_repo_is_blank(self):
        result = cd.compute({"a": hs(1, 2)})
        self.assertIsNone(result.stats["a"].duplicate_pct)
        self.assertIsNone(result.stats["a"].duplicate_commits)

    def test_an_empty_repo_is_blank_and_does_not_count_as_a_second_repo(self):
        alone = cd.compute({"a": hs(1, 2), "empty": []})
        self.assertIsNone(alone.stats["a"].duplicate_pct)
        self.assertIsNone(alone.stats["empty"].duplicate_pct)
        with_peer = cd.compute({"a": hs(1, 2), "b": hs(2, 3), "empty": []})
        self.assertEqual(with_peer.stats["a"].duplicate_pct, 50.0)
        self.assertIsNone(with_peer.stats["empty"].duplicate_pct)
        self.assertEqual(with_peer.stats["empty"].commits, 0)

    def test_a_commit_in_three_repos_is_counted_once_in_the_union(self):
        result = cd.compute({"a": hs(1, 2), "b": hs(1, 9), "c": hs(1, 8)})
        self.assertEqual(result.stats["a"].duplicate_commits, 1)
        self.assertEqual(result.stats["a"].duplicate_pct, 50.0)
        self.assertEqual({(p.repo_a, p.repo_b) for p in result.pairs}, {("a", "b"), ("a", "c"), ("b", "c")})

    def test_a_hash_repeated_inside_one_repo_is_one_commit(self):
        result = cd.compute({"a": [h(1), h(1), h(2)], "b": hs(1)})
        self.assertEqual(result.stats["a"].commits, 2)
        self.assertEqual(result.stats["a"].duplicate_pct, 50.0)

    def test_pairs_sort_by_shared_then_names_and_top_three_are_limited(self):
        sets = {
            "me": hs(1, 2, 3, 4, 5, 6, 7, 8, 9, 10),
            "big": hs(1, 2, 3, 4),
            "tie_b": hs(5, 6),
            "tie_a": hs(7, 8),
            "small": hs(9),
        }
        result = cd.compute(sets)
        self.assertEqual(result.stats["me"].top, [("big", 40.0), ("tie_a", 20.0), ("tie_b", 20.0)])
        order = [(p.repo_a, p.repo_b, p.shared_commits) for p in result.pairs]
        self.assertEqual(
            order,
            [("big", "me", 4), ("me", "tie_a", 2), ("me", "tie_b", 2), ("me", "small", 1)],
        )

    def test_result_does_not_depend_on_input_order(self):
        sets = {"a": hs(1, 2, 3), "b": hs(2, 3, 4), "c": hs(3, 4, 5)}
        forward = cd.compute(dict(sets))
        backward = cd.compute(dict(reversed(list(sets.items()))))
        self.assertEqual(forward, backward)

    def test_rounding_is_half_up_to_one_decimal(self):
        # 1 of 8 = 12.5 exactly; 1 of 16 = 6.25 -> 6.3 (not banker's 6.2)
        result = cd.compute({"a": hs(*range(1, 17)), "b": hs(1)})
        self.assertEqual(result.stats["a"].duplicate_pct, 6.3)

    def test_a_hash_that_is_not_40_hex_is_rejected(self):
        with self.assertRaises(ValueError):
            cd.compute({"a": ["not-a-hash"], "b": hs(1)})
        with self.assertRaises(ValueError):
            cd.compute({"a": [h(0xabc).upper()], "b": hs(1)})


class SummaryRowsTests(unittest.TestCase):
    def _rows(self):
        return [
            {"org": "o", "repo": "a", "loc": 10, "duplicate_pct": "stale", "duplicate_commits": "9", "duplicates_with": "x:1.0"},
            {"org": "o", "repo": "b", "loc": 20},
            {"org": "o", "repo": "failed", "loc": 0, "error": "boom"},
        ]

    def test_fields_are_appended_after_every_existing_column(self):
        self.assertEqual(
            ex.SUMMARY_FIELDS[-3:], ["duplicate_pct", "duplicate_commits", "duplicates_with"]
        )
        self.assertEqual(cd.DEDUP_FIELDS, ["duplicate_pct", "duplicate_commits", "duplicates_with"])

    def test_apply_sets_the_three_columns_and_keeps_everything_else(self):
        result = cd.compute({"A": hs(1, 2), "B": hs(2, 3, 4, 5)})
        names = {("o", "a"): "A", ("o", "b"): "B"}
        rows = self._rows()
        cd.apply_to_rows(rows, result, lambda row: names.get((row["org"], row["repo"])))
        self.assertEqual((rows[0]["duplicate_pct"], rows[0]["duplicate_commits"], rows[0]["duplicates_with"]),
                         ("50.0", 1, "B:50.0"))
        self.assertEqual((rows[1]["duplicate_pct"], rows[1]["duplicate_commits"], rows[1]["duplicates_with"]),
                         ("25.0", 1, "A:25.0"))
        self.assertEqual((rows[0]["loc"], rows[1]["loc"], rows[2]["error"]), (10, 20, "boom"))
        # a repo with no hashes (failed) is blank, never 0
        self.assertEqual([rows[2][f] for f in cd.DEDUP_FIELDS], ["", "", ""])

    def test_blank_clears_stale_values(self):
        rows = self._rows()
        cd.blank_rows(rows)
        for row in rows:
            self.assertEqual([row[f] for f in cd.DEDUP_FIELDS], ["", "", ""])
        self.assertEqual(rows[0]["loc"], 10)

    def test_a_repo_that_shares_nothing_gets_zero_and_an_empty_list(self):
        result = cd.compute({"A": hs(1), "B": hs(2)})
        rows = [{"org": "o", "repo": "a"}]
        cd.apply_to_rows(rows, result, lambda row: "A")
        self.assertEqual((rows[0]["duplicate_pct"], rows[0]["duplicate_commits"], rows[0]["duplicates_with"]), ("0.0", 0, ""))


class FilesTests(unittest.TestCase):
    def test_duplicates_csv_has_exact_header_and_rows(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "duplicates.csv"
            cd.write_duplicates_csv(path, [cd.Pair("a", "b", 3, 100.0, 66.7)])
            self.assertEqual(
                path.read_text(),
                "repo_a,repo_b,shared_commits,pct_of_a,pct_of_b\na,b,3,100.0,66.7\n",
            )
            cd.write_duplicates_csv(path, [])
            self.assertEqual(path.read_text(), "repo_a,repo_b,shared_commits,pct_of_a,pct_of_b\n")

    def test_hash_file_round_trip_is_sorted_unique_and_deterministic(self):
        with tempfile.TemporaryDirectory() as d:
            first, second = Path(d) / "1.gz", Path(d) / "2.gz"
            cd.write_hash_file(first, [h(3), h(1), h(3), h(2)])
            self.assertEqual(list(cd.iter_hash_file(first)), hs(1, 2, 3))
            cd.write_hash_file(second, [h(2), h(3), h(1)])
            self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_a_malformed_hash_file_is_an_error_not_silently_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "bad.gz"
            with gzip.open(path, "wt") as handle:
                handle.write(h(1) + "\nsome commit message\n")
            with self.assertRaises(ValueError):
                list(cd.iter_hash_file(path))

    def test_load_run_reads_only_named_repos_that_have_a_file(self):
        with tempfile.TemporaryDirectory() as d:
            run = Path(d)
            cd.write_hash_file(cd.hash_file_path(run, "a"), [h(1)])
            cd.write_hash_file(cd.hash_file_path(run, "orphan"), [h(1)])
            (run / "git" / "nofile").mkdir(parents=True)
            sources = cd.hash_sources(run, {"a": "A", "nofile": "N", "missing": "M"})
            self.assertEqual(sorted(sources), ["A"])


def _commit(repo: Path, name: str, message: str, date: str) -> None:
    (repo / name).write_text(message)
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", message, date=date)


def build_fixture(root: Path) -> None:
    """A (3 commits); B = clone of A plus 2 commits; C unrelated; D and E share first commit message and date
    but have different content, so they are not duplicates."""
    a = root / "A"
    a.mkdir()
    _git(a, "init", "-q", "-b", "main")
    for i in range(3):
        _commit(a, f"f{i}.txt", f"a{i}", f"2021-01-0{i + 1}T00:00:00")
    subprocess.run(["git", "clone", "-q", str(a), str(root / "B")], check=True, capture_output=True)
    _commit(root / "B", "extra1.txt", "b1", "2022-01-01T00:00:00")
    _commit(root / "B", "extra2.txt", "b2", "2022-01-02T00:00:00")
    for name, content in (("C", "c"), ("D", "same message d"), ("E", "same message e")):
        repo = root / name
        repo.mkdir()
        _git(repo, "init", "-q", "-b", "main")
    _commit(root / "C", "c.txt", "c-only", "2023-01-01T00:00:00")
    for name in ("D", "E"):
        (root / name / "file.txt").write_text(f"content of {name}")
        _git(root / name, "add", ".")
        _git(root / name, "commit", "-qm", "initial commit", date="2020-05-05T00:00:00")


class CollectCommitHashesTests(unittest.TestCase):
    def test_writes_every_reachable_hash_sorted_and_only_hashes(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            build_fixture(root)
            _git(root / "B", "checkout", "-qb", "side")
            _commit(root / "B", "side.txt", "SECRET-MESSAGE-TEXT", "2022-02-01T00:00:00")
            _git(root / "B", "checkout", "-q", "main")
            dest = root / "out" / "commit_hashes.txt.gz"
            count = ex.collect_commit_hashes(root / "B", dest, logging.getLogger("t"))
            expected = sorted(
                subprocess.run(["git", "-C", str(root / "B"), "rev-list", "--all"], capture_output=True, text=True,
                               check=True).stdout.split()
            )
            self.assertEqual(count, 6)  # 3 from A + 2 extra + the side branch
            with gzip.open(dest, "rt") as handle:
                lines = handle.read().splitlines()
            self.assertEqual(lines, expected)
            self.assertTrue(all(HEX40.match(line) for line in lines))
            self.assertNotIn("SECRET-MESSAGE-TEXT", gzip.open(dest, "rt").read())

    def test_a_directory_that_is_not_a_repo_returns_none_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d) / "out" / "commit_hashes.txt.gz"
            not_a_repo = Path(d) / "plain"
            not_a_repo.mkdir()
            with mock.patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": d}):
                self.assertIsNone(ex.collect_commit_hashes(not_a_repo, dest, logging.getLogger("t")))
            self.assertFalse(dest.exists())

    def test_a_timeout_returns_none_and_removes_a_stale_file(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            build_fixture(root)
            dest = root / "commit_hashes.txt.gz"
            cd.write_hash_file(dest, [h(1)])
            with mock.patch.object(ex.subprocess, "run", side_effect=subprocess.TimeoutExpired("git", 1)):
                self.assertIsNone(ex.collect_commit_hashes(root / "A", dest, logging.getLogger("t")))
            self.assertFalse(dest.exists())

    def test_an_empty_repo_gives_zero_commits_and_an_empty_file(self):
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / "empty"
            repo.mkdir()
            _git(repo, "init", "-q", "-b", "main")
            dest = Path(d) / "h.gz"
            self.assertEqual(ex.collect_commit_hashes(repo, dest, logging.getLogger("t")), 0)
            self.assertEqual(list(cd.iter_hash_file(dest)), [])


class RealGitComputeTests(unittest.TestCase):
    def test_copy_fork_unrelated_and_same_message_repos(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            build_fixture(root)
            sets = {}
            for name in "ABCDE":
                dest = root / "h" / f"{name}.gz"
                ex.collect_commit_hashes(root / name, dest, logging.getLogger("t"))
                sets[name] = list(cd.iter_hash_file(dest))
            result = cd.compute(sets)
            self.assertEqual(result.stats["A"].duplicate_pct, 100.0)
            self.assertEqual(result.stats["B"].duplicate_pct, 60.0)
            self.assertEqual(result.stats["B"].top, [("A", 60.0)])
            for name in "CDE":
                self.assertEqual(result.stats[name].duplicate_pct, 0.0, name)
            self.assertEqual(result.pairs, [cd.Pair("A", "B", 3, 100.0, 60.0)])


def run_main(argv: list[str]) -> tuple[int, str]:
    """Run the real CLI in-process (scc is stubbed: LOC is not under test and this keeps the run fast)."""
    buffer = io.StringIO()
    with mock.patch.object(sys, "argv", ["extract_org_raw_data.py", *argv]), \
         mock.patch.object(ex, "run_scc", return_value={"loc": 1, "by_language": {}}), \
         redirect_stdout(buffer):
        code = ex.main()
    return code, buffer.getvalue()


def read_summary(run_dir: Path) -> dict[str, dict[str, str]]:
    with (run_dir / "summary.csv").open(newline="", encoding="utf-8") as handle:
        return {row["repo"]: row for row in csv.DictReader(handle)}


def read_duplicates(run_dir: Path) -> list[dict[str, str]]:
    with (run_dir / "duplicates.csv").open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


class RunTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.repos = self.root / "repos"
        self.repos.mkdir()
        build_fixture(self.repos)
        self.out = self.root / "out"

    def _run(self, *extra: str) -> Path:
        code, _ = run_main(["--offline", "--local-repos-dir", str(self.repos), "--output-dir", str(self.out),
                            "--workers", "2", *extra])
        self.assertEqual(code, 0)
        return next(p for p in self.out.glob("raw-extract-*") if p.is_dir())

    def test_default_run_fills_summary_writes_duplicates_and_hash_files(self):
        run = self._run()
        summary = read_summary(run)
        self.assertEqual(list(summary["A"])[-3:], cd.DEDUP_FIELDS)
        self.assertEqual((summary["A"]["duplicate_pct"], summary["A"]["duplicate_commits"], summary["A"]["duplicates_with"]),
                         ("100.0", "3", "B:100.0"))
        self.assertEqual((summary["B"]["duplicate_pct"], summary["B"]["duplicate_commits"], summary["B"]["duplicates_with"]),
                         ("60.0", "3", "A:60.0"))
        for name in "CDE":
            self.assertEqual((summary[name]["duplicate_pct"], summary[name]["duplicate_commits"], summary[name]["duplicates_with"]),
                             ("0.0", "0", ""), name)
        self.assertEqual(
            read_duplicates(run),
            [{"repo_a": "A", "repo_b": "B", "shared_commits": "3", "pct_of_a": "100.0", "pct_of_b": "60.0"}],
        )
        for name in "ABCDE":
            lines = gzip.open(run / "git" / name / "commit_hashes.txt.gz", "rt").read().splitlines()
            self.assertTrue(lines and all(HEX40.match(line) for line in lines), name)
        self.assertEqual(len(list(cd.iter_hash_file(run / "git" / "B" / "commit_hashes.txt.gz"))), 5)

    def test_zip_and_manifest_include_duplicates_csv(self):
        run = self._run()
        with zipfile.ZipFile(run.with_suffix(".zip")) as zf:
            names = zf.namelist()
        self.assertIn(f"{run.name}/duplicates.csv", names)
        self.assertIn(f"{run.name}/git/B/commit_hashes.txt.gz", names)
        manifest = json.loads((run / "manifest.json").read_text())
        self.assertEqual(manifest["duplicates_csv"], str(run / "duplicates.csv"))
        self.assertEqual(manifest["commit_dedup"], {"enabled": True, "repos_compared": 5, "repos_sharing_commits": 2})

    def test_resume_recomputes_over_every_repo_not_only_the_ones_it_reran(self):
        run = self._run()
        # Simulate an interrupted run: B has no result yet and no hash file.
        job = json.loads((run / "job.json").read_text())
        job["repos"].pop("B")
        (run / "job.json").write_text(json.dumps(job))
        rows = list(csv.DictReader((run / "summary.csv").open(newline="")))
        with (run / "summary.csv").open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=ex.SUMMARY_FIELDS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(r for r in rows if r["repo"] != "B")
        (run / "git" / "B" / "commit_hashes.txt.gz").unlink()
        (run / "duplicates.csv").unlink()

        processed: list[str] = []
        real = ex.process_repo

        def spy(target, **kwargs):
            processed.append(target.full_name)
            return real(target, **kwargs)

        with mock.patch.object(ex, "process_repo", side_effect=spy):
            code, _ = run_main(["--resume", str(run), "--workers", "2"])
        self.assertEqual(code, 0)
        self.assertEqual(processed, ["B"])
        summary = read_summary(run)
        self.assertEqual(summary["A"]["duplicate_pct"], "100.0")  # A was not re-run, still gets its value
        self.assertEqual(summary["B"]["duplicate_pct"], "60.0")
        self.assertEqual(len(read_duplicates(run)), 1)

    def test_no_dedup_writes_no_files_and_leaves_the_columns_blank(self):
        run = self._run("--no-dedup")
        summary = read_summary(run)
        self.assertEqual(list(summary["A"])[-3:], cd.DEDUP_FIELDS)  # columns stay so the schema never changes
        for row in summary.values():
            self.assertEqual([row[f] for f in cd.DEDUP_FIELDS], ["", "", ""])
        self.assertFalse((run / "duplicates.csv").exists())
        self.assertEqual(list(run.rglob("commit_hashes.txt.gz")), [])
        manifest = json.loads((run / "manifest.json").read_text())
        self.assertNotIn("duplicates_csv", manifest)
        self.assertEqual(manifest["commit_dedup"], {"enabled": False})

    def test_no_dedup_on_resume_clears_values_from_an_earlier_run(self):
        run = self._run()
        code, _ = run_main(["--resume", str(run), "--no-dedup"])
        self.assertEqual(code, 0)
        for row in read_summary(run).values():
            self.assertEqual([row[f] for f in cd.DEDUP_FIELDS], ["", "", ""])
        self.assertFalse((run / "duplicates.csv").exists())

    def test_a_single_repo_run_does_not_compare(self):
        run = self._run("--local-repo", "A")
        row = read_summary(run)["A"]
        self.assertEqual([row[f] for f in cd.DEDUP_FIELDS], ["", "", ""])
        self.assertFalse((run / "duplicates.csv").exists())
        self.assertEqual(list(run.rglob("commit_hashes.txt.gz")), [])

    def test_a_rev_list_failure_blanks_that_repo_without_failing_it(self):
        real = ex.collect_commit_hashes

        def flaky(repo, dest, log):
            if repo.name == "B":
                return None
            return real(repo, dest, log)

        with mock.patch.object(ex, "collect_commit_hashes", side_effect=flaky):
            run = self._run()
        summary = read_summary(run)
        self.assertEqual(summary["B"]["error"], "")
        self.assertEqual([summary["B"][f] for f in cd.DEDUP_FIELDS], ["", "", ""])
        self.assertEqual(summary["A"]["duplicate_pct"], "0.0")  # B is not in the comparison at all

    def test_the_final_log_line_counts_repos_sharing_commits(self):
        log_text = None
        run = self._run()
        log_text = (run / "logs" / "extract.log").read_text()
        self.assertIn("Duplicate commits: 2 of 5 repos share commits with another repo (duplicates.csv)", log_text)

    def test_the_log_line_says_none_when_nothing_is_shared(self):
        run = self._run("--local-repo", "C", "--local-repo", "D")
        log_text = (run / "logs" / "extract.log").read_text()
        self.assertIn("Duplicate commits: none found", log_text)
        self.assertEqual(read_duplicates(run), [])


class UiCouplingTests(unittest.TestCase):
    """extract_ui.py reads the extractor's log lines by substring; the new line must not look like OK/FAIL/Extracting."""

    LINES = [
        "Duplicate commits: 2 of 5 repos share commits with another repo (duplicates.csv)",
        "Duplicate commits: none found",
    ]

    def test_the_new_log_lines_do_not_move_the_progress_bar(self):
        import extract_ui

        before = extract_ui.compute_progress(["Extracting 5 repos → /x", "OK a: merged_prs=0 loc=1 span_days=1"])
        after = extract_ui.compute_progress(
            ["Extracting 5 repos → /x", "OK a: merged_prs=0 loc=1 span_days=1", *self.LINES]
        )
        self.assertEqual(before, after)

    def test_the_new_log_lines_are_not_a_job_dir_marker(self):
        import extract_ui

        with mock.patch.object(extract_ui, "set_state") as set_state:
            for line in self.LINES:
                extract_ui.maybe_capture_job_dir(line)
        set_state.assert_not_called()

    def test_the_form_flag_defaults_on_and_can_be_switched_off(self):
        import extract_ui

        self.assertTrue(extract_ui.extract_form_settings({})["compare_commits"])
        self.assertTrue(extract_ui.extract_form_settings({"compare_commits_shown": ["1"], "compare_commits": ["on"]})["compare_commits"])
        self.assertFalse(extract_ui.extract_form_settings({"compare_commits_shown": ["1"]})["compare_commits"])

    def test_the_page_offers_the_checkbox_ticked_by_default(self):
        import extract_ui

        page = extract_ui.page()
        self.assertIn("Compare commit hashes between repos (duplicate %)", page)
        self.assertRegex(page, r'<input[^>]*name="compare_commits"[^>]*\bchecked\b')


if __name__ == "__main__":
    unittest.main()
