#!/usr/bin/env python3
"""Subversion metrics against a real throw-away repository (skipped when svn/svnadmin are missing).

The repository is a local ``file://`` one built with the real ``svn`` CLI, so parsing and line
counting are exercised against genuine output. Not covered: http(s)/svn:// servers, auth, and
certificate handling, which need a server.
"""
from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import extract_org_raw_data as ex
import svn_metrics as sv

HAVE_SVN = shutil.which("svn") is not None and shutil.which("svnadmin") is not None


def svn(*args: str, cwd: Path | None = None, user: str = "alice") -> str:
    return subprocess.run(
        ["svn", *args, "--non-interactive", "--no-auth-cache", "--username", user],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout


@unittest.skipUnless(HAVE_SVN, "svn / svnadmin not installed")
class SvnRepoCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        subprocess.run(["svnadmin", "create", str(root / "repo")], check=True)
        cls.url = (root / "repo").as_uri()
        svn("mkdir", "-m", "layout", f"{cls.url}/trunk", f"{cls.url}/branches", f"{cls.url}/tags")  # r1
        wc = root / "wc"
        svn("checkout", "-q", f"{cls.url}/trunk", str(wc))
        (wc / "src").mkdir()
        (wc / "src" / "a.py").write_text("one\ntwo\nthree\n")
        (wc / "logo.bin").write_bytes(b"\x00\x01\x02" * 100)
        svn("add", "-q", "src", "logo.bin", cwd=wc)
        svn("commit", "-q", "-m", "add files\n\nmore", cwd=wc)  # r2 alice
        (wc / "src" / "a.py").write_text("one\nTWO\nthree\nfour\nfive\n")
        svn("commit", "-q", "-m", "edit", cwd=wc, user="bob")  # r3: +3 -1 (git numstat style)
        svn("copy", "-m", "branch", f"{cls.url}/trunk", f"{cls.url}/branches/feat")  # r4
        bwc = root / "bwc"
        svn("checkout", "-q", f"{cls.url}/branches/feat", str(bwc))
        (bwc / "b.py").write_text("x\ny\n")
        svn("add", "-q", "b.py", cwd=bwc)
        svn("commit", "-q", "-m", "feature", cwd=bwc, user="bob")  # r5
        svn("update", "-q", cwd=wc)
        svn("merge", "-q", f"{cls.url}/branches/feat", cwd=wc)
        svn("commit", "-q", "-m", "merge feat", cwd=wc, user="ci-bot")  # r6 (mergeinfo + b.py)
        svn("rm", "-q", "src/a.py", cwd=wc)
        svn("commit", "-q", "-m", "remove", cwd=wc)  # r7: -5
        cls.root, cls.wc = root, wc
        cls.trunk = f"{cls.url}/trunk"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def conn(self) -> sv.SvnConn:
        return sv.SvnConn()


class TestHistory(SvnRepoCase):
    def test_info_and_revisions(self) -> None:
        info = sv.connect(self.conn(), self.trunk)
        self.assertEqual(info["relative"], "^/trunk")
        revs = list(sv.iter_revisions(self.conn(), self.trunk, info))
        # trunk is touched by r1 (created), r2, r3, r4 (copy source: not a change to trunk), r6, r7
        ids = [r["change"] for r in revs]
        self.assertEqual(ids, sorted(ids, reverse=True))
        self.assertIn(2, ids)
        self.assertNotIn(5, ids)  # a change on another branch is out of scope
        r2 = next(r for r in revs if r["change"] == 2)
        self.assertEqual((r2["user"], r2["subject"], r2["desc"]), ("alice", "add files", "add files\n\nmore"))
        self.assertTrue(r2["date"].endswith("+00:00"))

    def test_branch_scope_is_a_copy(self) -> None:
        url = f"{self.url}/branches/feat"
        revs = list(sv.iter_revisions(self.conn(), url, sv.connect(self.conn(), url)))
        self.assertTrue(sv.is_branch_copy(next(r for r in revs if r["change"] == 4)))
        self.assertFalse(sv.is_branch_copy(next(r for r in revs if r["change"] == 5)))

    def test_stats_and_bots(self) -> None:
        info = sv.connect(self.conn(), self.trunk)
        st = sv.aggregate_svn_stats(self.conn(), self.trunk, info, lambda n, e: n == "ci-bot")
        self.assertEqual(st["vcs"], "svn")
        self.assertEqual(st["bot_authors"], 1)
        self.assertEqual(st["human_authors"], 2)
        self.assertEqual(st["head_revision"], "7")


class TestChurn(SvnRepoCase):
    def export(self, **kw):
        info = sv.connect(self.conn(), self.trunk)
        changes = list(sv.iter_revisions(self.conn(), self.trunk, info))
        out = Path(self.tmp.name, f"c{len(kw)}.jsonl")
        n, merges = sv.export_commits_jsonl(self.conn(), self.trunk, out, changes=changes, is_bot=lambda n, e: False, **kw)
        return n, merges, {r["sha"]: r for r in map(json.loads, out.read_text().splitlines())}

    def test_line_counts(self) -> None:
        _, _, recs = self.export()
        self.assertEqual(recs["2"]["additions"], 3)  # binary file adds no lines
        self.assertEqual({f["path"] for f in recs["2"]["files"]}, {"src/a.py", "logo.bin"})
        self.assertEqual((recs["3"]["additions"], recs["3"]["deletions"]), (3, 1))
        self.assertEqual((recs["7"]["additions"], recs["7"]["deletions"]), (0, 5))

    def test_mergeinfo_revision_is_a_merge_without_files(self) -> None:
        _, merges, recs = self.export()
        self.assertEqual([m["sha"] for m in merges], ["6"])
        self.assertEqual(recs["6"]["files"], [])

    def test_cap_detail(self) -> None:
        _, _, recs = self.export(max_commits=2)
        self.assertTrue(recs["7"]["files"])
        self.assertEqual(recs["2"]["files"], [])


class TestTrees(SvnRepoCase):
    def test_export_head_has_no_metadata(self) -> None:
        dest = Path(self.tmp.name, "exp")
        n = sv.export_head(self.conn(), self.trunk, dest)
        self.assertEqual(n, 2)  # logo.bin and b.py (a.py was removed)
        self.assertFalse((dest / ".svn").exists())

    def test_workspace_check(self) -> None:
        svn("update", "-q", cwd=self.wc)  # a commit leaves the root at its old revision until updated
        self.assertTrue(sv.workspace_check(self.conn(), self.trunk, self.wc)["within_tolerance"])
        behind = Path(self.tmp.name, "behind")
        svn("checkout", "-q", "-r", "3", self.trunk, str(behind))
        chk = sv.workspace_check(self.conn(), self.trunk, behind)
        self.assertFalse(chk["within_tolerance"])
        self.assertEqual((chk["working_copy_revision"], chk["head_revision"]), (3, 7))
        (self.wc / "dirty.txt").write_text("x")
        svn("add", "-q", "dirty.txt", cwd=self.wc)
        try:
            self.assertFalse(sv.workspace_check(self.conn(), self.trunk, self.wc)["within_tolerance"])
        finally:
            svn("revert", "-q", "dirty.txt", cwd=self.wc)
            (self.wc / "dirty.txt").unlink()

    def test_tree_stats_ignores_svn_dir(self) -> None:
        files, _ = sv.tree_stats(self.wc)
        self.assertEqual(files, 2)

    def test_resolve_local_tree(self) -> None:
        with self.assertRaises(sv.SvnError):
            sv.resolve_local_tree(self.trunk, Path(self.tmp.name, "nowhere"))
        root = Path(self.tmp.name, "wcs")
        (root / sv.slug_for(self.trunk)).mkdir(parents=True)
        (root / sv.slug_for(self.trunk) / "f").write_text("x")
        self.assertEqual(sv.resolve_local_tree(self.trunk, root), root / sv.slug_for(self.trunk))

    def test_list_children(self) -> None:
        self.assertEqual(sv.list_children(self.conn(), self.url), ["branches", "tags", "trunk"])


class TestCliEndToEnd(SvnRepoCase):
    def run_cli(self, *extra: str, urls=None) -> Path:
        out = Path(tempfile.mkdtemp(dir=self.tmp.name))
        cmd = [sys.executable, str(Path(ex.__file__)), "--output-dir", str(out), "--no-for-check", *extra]
        for u in urls or [self.trunk]:
            cmd += ["--svn-url", u]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return next(p for p in out.iterdir() if p.is_dir())

    def check(self, run: Path) -> dict:
        with (run / "summary.csv").open() as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["error"], "")
        self.assertEqual(r["total_commits"], "5")  # r1, r2, r3, r6, r7
        self.assertEqual(r["merged_prs"], "1")
        self.assertEqual(r["measured_branch"], "^/trunk")
        self.assertEqual(r["duplicate_pct"], "")
        self.assertEqual(json.loads((run / "manifest.json").read_text())["vcs"], "subversion")
        self.assertTrue(any(run.glob("git/*/svn_stats.json")))
        self.assertTrue((run / "vcs_extra_metrics.csv").is_file())
        return r

    def test_export_mode(self) -> None:
        run = self.run_cli()
        r = self.check(run)
        self.assertEqual(r["total_files"], "2")
        self.assertFalse((run / "clones").exists() and any((run / "clones").iterdir()))

    def test_local_working_copy_mode(self) -> None:
        svn("update", "-q", cwd=self.wc)
        root = Path(tempfile.mkdtemp(dir=self.tmp.name))
        wc = root / sv.slug_for(self.trunk)
        svn("checkout", "-q", self.trunk, str(wc))
        run = self.run_cli("--svn-local-root", str(root))
        r = self.check(run)
        self.assertEqual(r["total_files"], "2")  # .svn metadata is not counted
        self.assertTrue((wc / ".svn").is_dir())  # the user's folder is left alone
        stats = json.loads(next(run.glob("git/*/svn_stats.json")).read_text())
        self.assertEqual(stats["tree_source"], "local working copy")
        self.assertTrue(stats["workspace_check"]["within_tolerance"])

    def test_branch_url_is_one_merge_row(self) -> None:
        run = self.run_cli(urls=[f"{self.url}/branches/feat"])
        with (run / "summary.csv").open() as fh:
            r = next(csv.DictReader(fh))
        self.assertEqual(r["error"], "")
        self.assertEqual(r["merged_prs"], "1")  # r4, the branch creation copy

    def test_rejects_credentials_in_url(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(Path(ex.__file__)), "--svn-url", "https://u:pw@host/r"],
            capture_output=True, text=True, timeout=60,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn(":pw@", proc.stdout + proc.stderr)


class TestUrlHandling(unittest.TestCase):
    def test_credentials_in_url_refused(self) -> None:
        for bad in ("https://u:p@host/svn/r", "ftp://host/r", "/just/a/path"):
            with self.assertRaises(ValueError):
                sv.normalize_url(bad)
        self.assertEqual(sv.normalize_url("https://host/svn/r/trunk/"), "https://host/svn/r/trunk")

    def test_password_goes_to_stdin_not_argv(self) -> None:
        args = sv.SvnConn(user="u", password="S3cret!").base()
        self.assertNotIn("S3cret!", " ".join(args))
        self.assertIn("--password-from-stdin", args)

    def test_parse_diff_ignores_property_lines(self) -> None:
        text = "Index: a\n===\n--- a\t(rev 1)\n+++ a\t(rev 2)\n@@ -1 +1 @@\n-x\n+y\n\nProperty changes on: .\n___\nAdded: svn:mergeinfo\n## -0,0 +0,1 ##\n   Merged /b:r1\n"
        files, merged = sv.parse_diff(text)
        self.assertEqual(files, {"a": [1, 1]})
        self.assertTrue(merged)


if __name__ == "__main__":
    unittest.main()
