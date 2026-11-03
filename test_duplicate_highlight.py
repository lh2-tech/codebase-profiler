"""Duplicate LOC/PR columns, per-PR open-source / AI-signature counts and the colour-coded workbooks.

Everything runs on synthetic repositories created in a temp dir; no network and no real client data."""

import csv
import gzip
import logging
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import commit_dedup as cd
import extract_org_raw_data as ex
import highlight
import pr_signals as ps

LOG = logging.getLogger("test")
MIT = "MIT License\n\nPermission is hereby granted, free of charge, to any person obtaining a copy of this software\n"


def h(n: int) -> str:
    return f"{n:040x}"


def git(repo: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Dev", "GIT_AUTHOR_EMAIL": "dev@example.com",
        "GIT_COMMITTER_NAME": "Dev", "GIT_COMMITTER_EMAIL": "dev@example.com",
    }
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env).stdout


def write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def make_repo(root: Path) -> Path:
    """main + PR #1 (app code, AI trailer) + PR #2 (code under a vendored MIT subtree) + PR #3 (plain human code)."""
    repo = root / "r"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    write(repo, "README.md", "hi\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")

    def pr(number: int, branch: str, files: dict[str, str], message: str) -> None:
        git(repo, "checkout", "-q", "-b", branch)
        for rel, text in files.items():
            write(repo, rel, text)
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", message)
        git(repo, "checkout", "-q", "main")
        git(repo, "merge", "-q", "--no-ff", branch, "-m", f"Merge pull request #{number} from o/{branch}")

    body = "".join(f"line {i}\n" for i in range(10))
    pr(1, "ai", {"src/app.py": body}, "add app\n\nCo-Authored-By: Claude <noreply@anthropic.com>")
    pr(2, "vendor", {"vendor/lib/x.py": body, "vendor/lib/LICENSE": MIT}, "vendor a lib")
    pr(3, "human", {"src/other.py": body}, "add other")
    return repo


class ThresholdTests(unittest.TestCase):
    def test_default_and_env(self):
        self.assertEqual(cd.threshold_pct({}), 25.0)
        self.assertEqual(cd.threshold_pct({"DUPLICATE_THRESHOLD_PCT": "40"}), 40.0)
        self.assertEqual(cd.threshold_pct({"DUPLICATE_THRESHOLD_PCT": " 12.5 "}), 12.5)

    def test_bad_values_fall_back(self):
        for bad in ("abc", "-1", "101", ""):
            self.assertEqual(cd.threshold_pct({"DUPLICATE_THRESHOLD_PCT": bad}), 25.0, bad)


class DuplicateDetailTests(unittest.TestCase):
    def setUp(self):
        # fork shares 3 of its 4 commits with parent (75%); parent shares 3 of 10 (30%); solo shares none
        self.hashes = {
            "parent": [h(i) for i in range(10)],
            "fork": [h(0), h(1), h(2), h(99)],
            "solo": [h(200), h(201)],
        }
        self.loc = {
            "parent": [(h(0), 5), (h(1), 7), (h(3), 100)],
            "fork": [(h(0), 5), (h(1), 7), (h(99), 40)],
        }
        self.prs = {"parent": [h(0), h(5)], "fork": [h(1), h(99)]}
        self.result = cd.compute(self.hashes, loc_sources=self.loc, pr_sources=self.prs)

    def test_loc_and_pr_counts_only_count_what_another_repo_holds(self):
        fork = self.result.stats["fork"]
        self.assertEqual((fork.duplicate_loc, fork.duplicate_prs), (12, 1))
        parent = self.result.stats["parent"]
        self.assertEqual((parent.duplicate_loc, parent.duplicate_prs), (12, 1))
        self.assertIsNone(self.result.stats["solo"].duplicate_loc)  # no source given: unknown, not 0

    def test_without_sources_they_are_unknown(self):
        plain = cd.compute(self.hashes)
        self.assertIsNone(plain.stats["fork"].duplicate_loc)
        self.assertIsNone(plain.stats["fork"].duplicate_prs)

    def _rows(self):
        return [{"org": "o", "repo": n} for n in ("parent", "fork", "solo")]

    def test_only_repos_above_the_threshold_get_the_detail_columns(self):
        rows = self._rows()
        cd.apply_to_rows(rows, self.result, lambda r: r["repo"], threshold=25.0)
        by = {r["repo"]: r for r in rows}
        self.assertEqual((by["fork"]["duplicate_of"], by["fork"]["duplicate_loc"], by["fork"]["duplicate_prs"]),
                         ("parent", 12, 1))
        self.assertEqual(by["parent"]["duplicate_of"], "fork")  # 30% > 25%
        self.assertEqual([by["solo"][f] for f in cd.DETAIL_FIELDS], ["", "", ""])

    def test_threshold_is_strictly_above(self):
        rows = self._rows()
        cd.apply_to_rows(rows, self.result, lambda r: r["repo"], threshold=30.0)  # parent is exactly 30.0
        by = {r["repo"]: r for r in rows}
        self.assertEqual(by["parent"]["duplicate_of"], "")
        self.assertEqual(by["fork"]["duplicate_of"], "parent")

    def test_raising_the_threshold_clears_the_flags(self):
        rows = self._rows()
        cd.apply_to_rows(rows, self.result, lambda r: r["repo"], threshold=90.0)
        self.assertTrue(all(r["duplicate_of"] == "" for r in rows))

    def test_loc_file_roundtrip_and_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "commit_loc.txt.gz"
            cd.write_loc_file(path, {h(2): 3, h(1): 4, h(3): 0})
            self.assertEqual(list(cd.iter_loc_file(path)), [(h(1), 4), (h(2), 3)])  # sorted, zero dropped
            with self.assertRaises(ValueError):
                cd.write_loc_file(path, {"not-a-hash": 1})
            with gzip.open(path, "wt") as fh:
                fh.write(f"{h(1)} abc\n")
            with self.assertRaises(ValueError):
                list(cd.iter_loc_file(path))


class PrFieldTests(unittest.TestCase):
    def test_with_pr_counts(self):
        self.assertEqual(ps.with_pr_counts(12.5, ps.Tally(5, 3, 0)), "12.5,3")
        self.assertEqual(ps.with_pr_counts(12.5, ps.Tally(5, 3, 2)), "12.5,3,>50%:2")
        self.assertEqual(ps.with_pr_counts("", ps.Tally(5, 0, 0)), "-,0")
        self.assertEqual(ps.with_pr_counts('["a, b"]', ps.Tally(5, 1, 1)), '["a, b"],1,>50%:1')
        self.assertEqual(ps.with_pr_counts(12.5, None), 12.5)  # not analysed: untouched

    def test_read_and_strip_roundtrip(self):
        for existing in (12.5, "-", '["a, 5"]', '["x"]'):
            out = ps.with_pr_counts(existing, ps.Tally(9, 4, 2))
            self.assertEqual(ps.read_pr_counts(out), (4, 2))
            self.assertEqual(ps.strip_pr_counts(out), str(existing))
        self.assertIsNone(ps.read_pr_counts("12.5"))
        self.assertIsNone(ps.read_pr_counts('["a,5"]'))  # a comma inside the JSON is not a count

    def test_majority_is_strictly_more_than_half(self):
        sigs = [ps.PrSignal("1", h(1), 10, 5, 0, 0), ps.PrSignal("2", h(2), 10, 6, 0, 0),
                ps.PrSignal("3", h(3), 0, 0, 0, 0)]
        t = ps.tally(sigs, "oss")
        self.assertEqual((t.prs, t.affected, t.majority), (3, 2, 1))

    def test_ai_signature(self):
        self.assertTrue(ps.carries_ai_signature("Dev <d@x.com>", "x\n\nCo-Authored-By: Claude <noreply@anthropic.com>"))
        self.assertTrue(ps.carries_ai_signature("Dev <d@x.com>", "x\n\n\U0001F916 Generated with Claude Code"))
        self.assertTrue(ps.carries_ai_signature("Copilot <copilot-swe-agent[bot]@x.com>", "fix"))
        self.assertFalse(ps.carries_ai_signature("Dev <d@x.com>", "refactor the claude_client module"))


@unittest.skipUnless(subprocess.run(["git", "--version"], capture_output=True).returncode == 0, "git missing")
class GitFixtureTests(unittest.TestCase):
    def test_per_pr_signals_from_a_real_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp))
            dest = Path(tmp) / "pr_signals.json"
            signals = ex.collect_pr_signals(repo, dest, LOG)
            self.assertIsNotNone(signals)
            by = {s.number: s for s in signals}
            self.assertEqual(set(by), {"1", "2", "3"})
            # PR 1: all of it in one AI-signed commit, no open source
            self.assertEqual((by["1"].changed_loc, by["1"].oss_loc, by["1"].ai_loc, by["1"].ai_total_loc), (10, 0, 10, 10))
            # PR 2: the vendored lib (LICENSE is not a code file) is all open source, no AI signature
            self.assertEqual((by["2"].changed_loc, by["2"].oss_loc, by["2"].ai_loc), (10, 10, 0))
            # PR 3: neither
            self.assertEqual((by["3"].oss_loc, by["3"].ai_loc), (0, 0))
            oss, ai = ps.tally(signals, "oss"), ps.tally(signals, "ai")
            self.assertEqual((oss.affected, oss.majority, ai.affected, ai.majority), (1, 1, 1, 1))
            self.assertEqual(len(ps.read_file(dest)), 3)
            text = dest.read_text()
            self.assertNotIn("vendor", text)  # numbers and hashes only: no paths
            self.assertNotIn("Claude", text)

    def test_squash_commit_is_all_or_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "s"
            repo.mkdir()
            git(repo, "init", "-q", "-b", "main")
            write(repo, "a.py", "x\n")
            git(repo, "add", "-A")
            git(repo, "commit", "-q", "-m", "init")
            write(repo, "b.py", "1\n2\n3\n")
            git(repo, "add", "-A")
            git(repo, "commit", "-q", "-m", "feature (#7)\n\nCo-authored-by: Copilot <copilot@github.com>")
            signals = ex.collect_pr_signals(repo, Path(tmp) / "out.json", LOG)
            self.assertEqual([(s.number, s.ai_loc, s.ai_total_loc) for s in signals], [("7", 3, 3)])

    def test_commit_loc_counts_only_added_code_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp))
            dest = Path(tmp) / "commit_loc.txt.gz"
            self.assertIsNotNone(ex.collect_commit_loc(repo, dest, LOG))
            added = dict(cd.iter_loc_file(dest))
            self.assertEqual(sorted(added.values()), [10, 10, 10])  # the three PR commits; README and LICENSE are not code

    def test_budget_overrun_publishes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp))
            records = ex.detect_merged_prs_from_git(repo)
            ticks = iter(range(0, 10_000, 100))
            out = ps.analyse(records, lambda *a: "", is_oss_path=lambda p: False, is_code=lambda p: True,
                             budget_seconds=150, now=lambda: next(ticks))
            self.assertIsNone(out)


def _row(**kw):
    base = {"org": "o", "subgroup": "", "project_name": "o", "repo": "r", "loc": "100", "error": ""}
    base.update(kw)
    return base


class HighlightTests(unittest.TestCase):
    def test_predicates(self):
        self.assertTrue(highlight.is_duplicate(_row(duplicate_of="o/x")))
        self.assertFalse(highlight.is_duplicate(_row(duplicate_of="")))
        self.assertTrue(highlight.is_open_source(_row(open_source_loc_pct="0.0,2")))
        self.assertTrue(highlight.is_open_source(_row(open_source_loc_pct="3.5")))
        self.assertFalse(highlight.is_open_source(_row(open_source_loc_pct="0.0,0")))
        self.assertFalse(highlight.is_open_source(_row(open_source_loc_pct="")))
        self.assertTrue(highlight.is_vibe_coded(_row(vibe_code_signals='["possible scaffolding"],0')))
        self.assertTrue(highlight.is_vibe_coded(_row(vibe_code_signals="-,4")))
        self.assertFalse(highlight.is_vibe_coded(_row(vibe_code_signals="-,0")))
        self.assertFalse(highlight.is_vibe_coded(_row(vibe_code_signals="[],0")))
        self.assertFalse(highlight.is_vibe_coded(_row(vibe_code_signals="")))

    def test_summary_fills_colour_duplicates_and_only_majority_cells(self):
        header = ["repo", "open_source_loc_pct", "vibe_code_signals", *cd.ALL_FIELDS]
        dup = ["r", "1.0,3", "-,2", "60.0", "6", "x", "o/x", "100", "2"]
        fills = highlight.summary_fills(header, dup)
        self.assertEqual(fills[0], highlight.DUPLICATE_COLOR)
        self.assertEqual(fills[header.index("duplicate_prs")], highlight.DUPLICATE_COLOR)
        self.assertNotIn(1, fills)  # affected PRs but none more than half: not individually coloured
        self.assertNotIn(2, fills)
        major = ["r", "1.0,3,>50%:1", "-,2,>50%:2", "", "", "", "", "", ""]
        fills = highlight.summary_fills(header, major)
        self.assertEqual(fills, {1: highlight.OPEN_SOURCE_COLOR, 2: highlight.VIBE_COLOR})


class WorkbookTests(unittest.TestCase):
    def _summary(self, tmp: Path) -> Path:
        header = ["org", "subgroup", "project_name", "repo", "loc", "primary_language", "merged_prs", "total_commits",
                  "contributor_count", "open_source_loc_pct", "vibe_code_signals", "error", *cd.ALL_FIELDS]
        rows = [
            {"org": "a", "project_name": "a", "repo": "dup", "loc": 10, "open_source_loc_pct": "2.0,0",
             "vibe_code_signals": "-,0", "duplicate_pct": "60.0", "duplicate_of": "a/other", "duplicate_loc": 40,
             "duplicate_prs": 2},
            {"org": "a", "project_name": "a", "repo": "oss", "loc": 20, "open_source_loc_pct": "9.0,4,>50%:1",
             "vibe_code_signals": "-,0"},
            {"org": "a", "project_name": "a", "repo": "vibe", "loc": 30, "open_source_loc_pct": "0.0,0",
             "vibe_code_signals": '["possible generated scaffolding"],0'},
            {"org": "a", "project_name": "a", "repo": "clean", "loc": 40, "open_source_loc_pct": "0.0,0",
             "vibe_code_signals": "-,0"},
            {"org": "a", "project_name": "a", "repo": "failed", "loc": 0, "error": "boom",
             "open_source_loc_pct": "5.0,1"},
        ]
        path = tmp / "summary.csv"
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=header, restval="")
            writer.writeheader()
            writer.writerows(rows)
        return path

    def test_project_compiled_colours_and_legend(self):
        import openpyxl

        with tempfile.TemporaryDirectory() as tmp:
            out = highlight.write_project_compiled(self._summary(Path(tmp)))
            self.assertEqual(out.name, "project_compiled.xlsx")
            sheet = openpyxl.load_workbook(out)["project compiled"]
            header = [c.value for c in sheet[1][: len(highlight.COMPILED_COLUMNS)]]
            col = {n: i + 1 for i, n in enumerate(header)}
            by_repo = {sheet.cell(row=r, column=col["repo"]).value: r for r in range(2, 7)}

            def color(repo, name):
                rgb = sheet.cell(row=by_repo[repo], column=col[name]).fill.start_color.rgb
                return rgb[-6:] if sheet.cell(row=by_repo[repo], column=col[name]).fill.fill_type else None

            self.assertEqual(color("dup", "repo"), highlight.DUPLICATE_COLOR)
            self.assertEqual(color("dup", "duplicate_of"), highlight.DUPLICATE_COLOR)
            self.assertEqual(color("oss", "open_source_loc_pct"), highlight.OPEN_SOURCE_COLOR)
            self.assertEqual(color("oss", "repo"), highlight.OPEN_SOURCE_COLOR)
            self.assertEqual(color("vibe", "vibe_code_signals"), highlight.VIBE_COLOR)
            self.assertIsNone(color("clean", "repo"))
            self.assertIsNone(color("failed", "open_source_loc_pct"))  # a failed repo is never highlighted
            # a repo positive for duplicate AND open source keeps one colour per signal
            self.assertEqual(color("dup", "open_source_loc_pct"), highlight.OPEN_SOURCE_COLOR)
            # the legend sits in column Z and names every colour
            legend = {sheet.cell(row=r, column=26).fill.start_color.rgb[-6:]: sheet.cell(row=r, column=26).value
                      for r in range(2, 5)}
            self.assertIn("Duplicate", legend[highlight.DUPLICATE_COLOR])
            self.assertIn("Open source", legend[highlight.OPEN_SOURCE_COLOR])
            self.assertIn("Vibe", legend[highlight.VIBE_COLOR])
            self.assertEqual(sheet["Z1"].value, "Colour key")

    def test_summary_workbook_marks_duplicate_rows_and_majority_cells(self):
        import openpyxl
        import for_check

        with tempfile.TemporaryDirectory() as tmp:
            path = self._summary(Path(tmp))
            xlsx = for_check.write_workbook(path, include_for_check=False)
            sheet = openpyxl.load_workbook(xlsx)["summary"]
            header = [c.value for c in sheet[1]]
            col = {n: i + 1 for i, n in enumerate(header)}
            rows = {sheet.cell(row=r, column=col["repo"]).value: r for r in range(2, 7)}

            def filled(repo, name):
                cell = sheet.cell(row=rows[repo], column=col[name])
                return cell.fill.start_color.rgb[-6:] if cell.fill.fill_type else None

            self.assertEqual(filled("dup", "repo"), highlight.DUPLICATE_COLOR)
            self.assertEqual(filled("dup", "duplicate_loc"), highlight.DUPLICATE_COLOR)
            self.assertEqual(filled("oss", "open_source_loc_pct"), highlight.OPEN_SOURCE_COLOR)
            self.assertIsNone(filled("vibe", "vibe_code_signals"))  # signals but no PR more than half affected
            self.assertIsNone(filled("clean", "repo"))
            self.assertEqual(sheet.cell(row=rows["oss"], column=col["loc"]).value, 20)  # numbers stay numbers

    def test_summary_workbook_headers_explain_the_cell_format(self):
        import openpyxl
        import for_check

        with tempfile.TemporaryDirectory() as tmp:
            xlsx = for_check.write_workbook(self._summary(Path(tmp)), include_for_check=False)
            sheet = openpyxl.load_workbook(xlsx)["summary"]
            notes = {c.value: c.comment.text for c in sheet[1] if c.comment}
            self.assertIn("PRs affected", notes["open_source_loc_pct"])
            self.assertIn(">50%", notes["vibe_code_signals"])
            self.assertIn("LLM", notes["vibe_code_signals"])
            self.assertIn("blank", notes["duplicate_pct"].lower())
            for name in cd.DETAIL_FIELDS:
                self.assertIn("flagged", notes[name].lower())
            self.assertNotIn("repo", notes)  # only the columns that need explaining
            self.assertEqual(sheet.cell(row=2, column=1).comment, None)  # data rows carry no notes

    def test_project_compiled_explains_the_cell_format_below_the_colour_key(self):
        import openpyxl

        with tempfile.TemporaryDirectory() as tmp:
            sheet = openpyxl.load_workbook(highlight.write_project_compiled(self._summary(Path(tmp))))[
                "project compiled"]
            key = [sheet.cell(row=r, column=highlight.LEGEND_COLUMN).value for r in range(1, sheet.max_row + 1)]
            self.assertIn("How to read the cells", key)
            text = "\n".join(v for v in key if v)
            self.assertIn("12.5,7,>50%:2", text)
            self.assertIn("LLM was off", text)
            self.assertIn("not compared", text)
            header = {c.value: c.comment.text for c in sheet[1] if c.comment}
            self.assertIn("PRs affected", header["open_source_loc_pct"])


if __name__ == "__main__":
    unittest.main()
