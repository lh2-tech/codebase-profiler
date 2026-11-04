#!/usr/bin/env python3
"""vcs_extra: change shape, activity, authorship, message quality and the tree inventory."""

from __future__ import annotations

import csv
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import vcs_extra as vx

NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)


def commit(day: int, author: str, lines: int = 3, files: int = 1, subject: str = "add thing", bot: bool = False) -> dict:
    when = NOW - timedelta(days=day)
    fl = [{"path": f"f{i}", "additions": lines // max(files, 1), "deletions": 0} for i in range(files)] if lines else []
    return {"author_name": author, "author_date": when.isoformat(), "subject": subject, "is_bot": bot,
            "files": fl, "additions": sum(f["additions"] for f in fl), "deletions": 0}


class ShapeTests(unittest.TestCase):
    def test_change_shape_ignores_changes_without_detail_and_counts_mega(self) -> None:
        cs = [commit(1, "a", 10), commit(2, "a", 20), commit(3, "a", 2000), commit(4, "a", 0)]  # last: merge/no files
        s = vx.change_shape(cs)
        self.assertEqual(s["changes_with_detail"], 3)
        self.assertEqual(s["median_lines_per_change"], 20)
        self.assertEqual(s["mega_changes"], 1)
        self.assertEqual(s["max_lines_in_a_change"], 2000)

    def test_activity_windows_and_gaps(self) -> None:
        cs = [commit(5, "a"), commit(40, "b"), commit(200, "a"), commit(600, "c"), commit(1, "bot", bot=True)]
        a = vx.activity(cs, now=NOW)
        self.assertEqual(a["commits_last_90d"], 3)
        self.assertEqual(a["commits_last_365d"], 4)
        self.assertEqual(a["active_authors_90d"], 2)  # the bot is not an author
        self.assertEqual(a["longest_gap_days"], 400)

    def test_epoch_zero_dates_are_ignored(self) -> None:
        c = commit(1, "a")
        c["author_date"] = "1970-01-01T00:00:00+00:00"
        self.assertEqual(vx.activity([c, commit(2, "a")], now=NOW)["active_months"], 1)

    def test_authorship_concentration(self) -> None:
        cs = [commit(i, "alice") for i in range(1, 9)] + [commit(20, "bob"), commit(21, "cara"), commit(22, "bot", bot=True)]
        a = vx.authorship(cs)
        self.assertEqual(a["top_author_share_pct"], 80.0)  # the bot is not counted
        self.assertEqual(a["authors_for_80pct_of_commits"], 1)
        self.assertEqual(a["authors_with_5pct_or_more"], 3)

    def test_message_quality(self) -> None:
        subjects = ["", "<enter description here>", "wip", "fix login bug (#12 review: 4411)", "Reviewed-by: Ana", "refactor parser"]
        m = vx.message_quality([commit(i, "a", subject=s) for i, s in enumerate(subjects, 1)])
        self.assertEqual(m["generic_message_pct"], 50.0)
        self.assertEqual(m["review_tagged_pct"], 33.3)


class InventoryTests(unittest.TestCase):
    def test_binary_share_and_skips_vcs_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "a.py").write_text("print(1)\n")
            (root / "img.png").write_bytes(b"\x89PNG\x00" + bytes(range(256)) * 4)
            (root / "empty.txt").write_text("")
            (root / ".hg").mkdir()
            (root / ".hg" / "store").write_bytes(b"x" * 5000)
            inv = vx.tree_inventory(root)
            self.assertEqual((inv["files"], inv["text_files"], inv["binary_files"], inv["zero_byte_files"]), (3, 1, 1, 1))
            self.assertGreater(inv["binary_bytes_pct"], 95)
            self.assertEqual(inv["largest_files"][0]["path"], "img.png")
            self.assertEqual(inv["top_extensions_by_bytes"][0]["ext"], ".png")


class RollUpTests(unittest.TestCase):
    def test_collect_flatten_and_csv(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "tree").mkdir()
            (root / "tree" / "a.py").write_text("x=1\n")
            jl = root / "commits.jsonl"
            jl.write_text("\n".join(json.dumps(commit(i, "a")) for i in range(1, 4)) + "\n{bad json\n")
            rec = vx.collect(repo="depot/x", vcs="p4", tree=root / "tree", commits_jsonl=jl,
                             stats={"total_commits": 3, "integration_changelists": 1},
                             extra={"branch_copy_pct": 40.0, "integration_sources": ["//depot/main (5)"]})
            self.assertEqual(rec["merge_or_integration_pct"], 33.3)
            run = root / "run"
            (run / "git" / "depot__x").mkdir(parents=True)
            (run / "git" / "depot__x" / "vcs_extra.json").write_text(json.dumps(rec))
            path = vx.write_run_csv(run, vx.load_run_records(run))
            with path.open() as fh:
                row = next(csv.DictReader(fh))
            self.assertEqual((row["repo"], row["branch_copy_pct"], row["integration_sources"]), ("depot/x", "40.0", "//depot/main (5)"))
            self.assertEqual(row["files"], "1")


if __name__ == "__main__":
    unittest.main()
