#!/usr/bin/env python3
"""Plastic SCM metrics against a stub ``cm`` (no Plastic server is available to test with).

The stub answers the exact commands ``plastic_metrics`` issues, from a small in-memory repository, so
these tests prove parsing, line counting, merge handling, credential hygiene and the CLI wiring, but
NOT that a real server prints what the stub prints.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import extract_org_raw_data as ex
import plastic_metrics as pl

STUB = r'''#!/usr/bin/env python3
import os, re, sys
FS, RS = "\x1f", "\x1e"
a = sys.argv[1:]
log = os.environ.get("CM_STUB_LOG")
if log:
    open(log, "a").write(" ".join(a) + "\nHOME=" + os.environ.get("HOME", "") + "\n")
CS = {  # id: (owner, date, branch, comment)
    0: ("alice@x.io", "2024-01-01T10:00:00", "/main", "initial\nsecond line"),
    1: ("alice@x.io", "2024-01-02T10:00:00", "/main", "edit"),
    2: ("bob", "2024-01-03T10:00:00", "/main/feat", "feature work"),
    3: ("bob", "2024-01-04T10:00:00", "/main", "merge feat"),
    4: ("ci-bot", "2024-01-05T10:00:00", "/main", "remove"),
}
def fmt():
    return [x for x in a if x.startswith("--format=")][0][9:]
cmd = a[0]
if cmd == "configure":
    if os.environ.get("CM_STUB_BAD_AUTH"):
        sys.stderr.write("bad password " + [x for x in a if x.startswith("--password=")][0][11:]); sys.exit(1)
    sys.exit(0)
if cmd == "repository":
    if a[1] == "list":
        print("game\nlibs"); sys.exit(0)
if cmd == "find":
    obj = a[1]
    where = next((x for x in a if x.startswith("where ")), "")
    if obj == "changeset":
        fields = re.findall(r"\{(\w+)\}", fmt())
        for i, (o, d, b, c) in CS.items():
            if "changesetid = 0" in where and i != 0:
                continue
            v = {"changesetid": str(i), "owner": o, "date": d, "branch": b, "comment": c}
            sys.stdout.write(FS.join(v[f] for f in fields) + RS + "\n")
    elif obj == "merge":
        sys.stdout.write("3" + RS + "\n")
    elif obj == "branch":
        sys.stdout.write("/main" + RS + "\n/main/feat" + RS + "\n")
    sys.exit(0)
if cmd == "diff":
    n = int(a[1].split(":")[1].split("@")[0])
    rows = {
        0: [("A", "F", "10", "-1", "/src/a.py"), ("A", "B", "11", "-1", "/img.png"), ("A", "D", "9", "-1", "/src")],
        1: [("C", "F", "12", "10", "/src/a.py")],
        2: [("A", "F", "13", "-1", "/src/b.py")],
        4: [("D", "F", "14", "13", "/src/b.py"), ("M", "F", "15", "15", "/src/a.py")],
    }.get(n, [])
    for r in rows:
        sys.stdout.write(FS.join(r) + RS + "\n")
    sys.exit(0)
if cmd == "diffmetrics":
    sys.stdout.write("1" + FS + "2" + FS + "0")  # changed, added, deleted
    sys.exit(0)
if cmd == "cat":
    rev = a[1].split(":")[1].split("@")[0]
    sys.stdout.buffer.write({"10": b"l1\nl2\nl3\n", "11": b"\x89PNG\x00\x00", "13": b"x\ny"}.get(rev, b""))
    sys.exit(0)
if cmd == "workspace":
    if a[1] == "create":
        os.makedirs(a[3], exist_ok=True)
    sys.exit(0)
if cmd == "update":
    open(os.path.join(a[1], "main.py"), "w").write("print(1)\n")
    sys.exit(0)
sys.exit(0)
'''


class PlasticStubCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.bin = Path(cls.tmp.name, "bin")
        cls.bin.mkdir()
        cm = cls.bin / "cm"
        cm.write_text(STUB)
        cm.chmod(cm.stat().st_mode | stat.S_IEXEC)
        cls._path = os.environ["PATH"]
        os.environ["PATH"] = f"{cls.bin}{os.pathsep}{cls._path}"

    @classmethod
    def tearDownClass(cls) -> None:
        os.environ["PATH"] = cls._path
        cls.tmp.cleanup()

    def conn(self, **kw) -> pl.PlasticConn:
        return pl.PlasticConn(server="localhost:8087", **kw)


class TestHistory(PlasticStubCase):
    def test_changesets_newest_first_multiline_comment(self) -> None:
        cs = list(pl.iter_changesets(self.conn(), "game"))
        self.assertEqual([c["change"] for c in cs], [4, 3, 2, 1, 0])
        self.assertEqual(cs[-1]["subject"], "initial")
        self.assertEqual(cs[-1]["desc"], "initial\nsecond line")
        self.assertEqual(cs[-1]["date"], "2024-01-01T10:00:00+00:00")

    def test_stats_and_bots(self) -> None:
        conn = self.conn()
        stats = pl.aggregate_plastic_stats(conn, "game", lambda n, e: n == "ci-bot")
        self.assertEqual(stats["total_commits"], 5)
        self.assertEqual(stats["human_authors"], 2)
        self.assertEqual(stats["bot_authors"], 1)
        self.assertEqual(stats["branch_count"], 2)
        self.assertEqual(stats["vcs"], "plastic")
        alice = next(a for a in stats["authors"] if a["name"] == "alice@x.io")
        self.assertEqual(alice["email"], "alice@x.io")

    def test_main_branch(self) -> None:
        self.assertEqual(pl.main_branch(self.conn(), "game"), "/main")

    def test_merge_destinations(self) -> None:
        self.assertEqual(pl.merge_destinations(self.conn(), "game"), {3})


class TestChurn(PlasticStubCase):
    def export(self, max_commits: int = 0):
        conn = self.conn()
        changes = list(pl.iter_changesets(conn, "game"))
        out = Path(self.tmp.name, f"c{max_commits}.jsonl")
        n, merges = pl.export_commits_jsonl(
            conn, "game", out, changes=changes, merges=pl.merge_destinations(conn, "game"),
            is_bot=lambda n, e: False, max_commits=max_commits,
        )
        return n, merges, {r["sha"]: r for r in map(json.loads, out.read_text().splitlines())}

    def test_line_counts(self) -> None:
        n, merges, recs = self.export()
        self.assertEqual(n, 5)
        # added text file counted with cat, binary + directory contribute nothing
        self.assertEqual(recs["0"]["additions"], 3)
        self.assertEqual({f["path"] for f in recs["0"]["files"]}, {"src/a.py", "img.png"})
        # changed file: changed(1)+added(2) / changed(1)+deleted(0)
        self.assertEqual((recs["1"]["additions"], recs["1"]["deletions"]), (3, 1))
        # no trailing newline still counts the last line
        self.assertEqual(recs["2"]["additions"], 2)
        # deleted file counted from its parent revision; a move is 0/0
        self.assertEqual((recs["4"]["additions"], recs["4"]["deletions"]), (0, 2))

    def test_merge_changeset_has_no_files(self) -> None:
        _, merges, recs = self.export()
        self.assertEqual(recs["3"]["files"], [])
        self.assertEqual([m["sha"] for m in merges], ["3"])

    def test_cap_detail(self) -> None:
        _, _, recs = self.export(max_commits=2)
        self.assertTrue(recs["4"]["files"])
        self.assertEqual(recs["2"]["files"], [])  # past the cap: counted, no per-file churn


class TestConnectionAndTree(PlasticStubCase):
    def test_password_uses_private_home_and_is_removed(self) -> None:
        log = Path(self.tmp.name, "cm.log")
        os.environ["CM_STUB_LOG"] = str(log)
        conn = self.conn(user="u", password="S3cret!")
        try:
            pl.connect(conn)
            home = conn.home
            self.assertTrue(home and Path(home, ".plastic4").is_dir())
            self.assertNotEqual(home, os.environ.get("HOME"))
            self.assertIn(f"HOME={home}", log.read_text())
            self.assertNotIn("S3cret!", repr(conn))
        finally:
            os.environ.pop("CM_STUB_LOG")
            pl.close(conn)
        self.assertFalse(Path(home).exists())

    def test_failed_auth_error_hides_password(self) -> None:
        os.environ["CM_STUB_BAD_AUTH"] = "1"
        try:
            with self.assertRaises(pl.PlasticError) as cm:
                pl.connect(self.conn(user="u", password="S3cret!"))
        finally:
            os.environ.pop("CM_STUB_BAD_AUTH")
        self.assertNotIn("S3cret!", str(cm.exception))

    def test_no_password_leaves_home_alone(self) -> None:
        conn = self.conn()
        pl.connect(conn)
        self.assertEqual(conn.home, "")

    def test_list_repositories(self) -> None:
        self.assertEqual(pl.list_repositories(self.conn()), ["game", "libs"])

    def test_sync_working_tree(self) -> None:
        dest = Path(self.tmp.name, "tree")
        self.assertEqual(pl.sync_working_tree(self.conn(), "game", dest), 1)
        self.assertTrue((dest / "main.py").is_file())

    def test_local_tree_resolution(self) -> None:
        root = Path(self.tmp.name, "wk")
        (root / "game").mkdir(parents=True)
        with self.assertRaises(pl.PlasticError):
            pl.resolve_local_tree("game", root)  # empty folder: never silently download
        (root / "game" / "f.txt").write_text("x")
        self.assertEqual(pl.resolve_local_tree("game", root), root / "game")
        with self.assertRaises(pl.PlasticError):
            pl.resolve_local_tree("libs", root)

    def test_tree_stats_ignores_plastic_metadata(self) -> None:
        t = Path(self.tmp.name, "ts")
        (t / ".plastic").mkdir(parents=True)
        (t / ".plastic" / "x").write_text("meta")
        (t / "ignore.conf").write_text("meta")
        (t / "a.txt").write_text("abc")
        self.assertEqual(pl.tree_stats(t), (1, 3))


class TestCliEndToEnd(PlasticStubCase):
    def run_cli(self, *extra: str) -> Path:
        out = Path(tempfile.mkdtemp(dir=self.tmp.name))
        env = {**os.environ, "PLASTIC_PASSWORD": "S3cret!"}
        proc = subprocess.run(
            [sys.executable, str(Path(ex.__file__)), "--plastic-server", "localhost:8087", "--plastic-user", "alice",
             "--plastic-repo", "game", "--output-dir", str(out), "--no-for-check", *extra],
            capture_output=True, text=True, env=env, timeout=300,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return next(p for p in out.iterdir() if p.is_dir())

    def check(self, run: Path) -> dict:
        import csv
        rows = list(csv.DictReader((run / "summary.csv").open()))
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["error"], "")
        self.assertEqual((r["total_commits"], r["merged_prs"], r["local_merges"]), ("5", "", "1"))
        self.assertEqual(r["measured_branch"], "/main")
        self.assertEqual(r["duplicate_pct"], "")
        self.assertEqual(json.loads((run / "manifest.json").read_text())["vcs"], "plastic")
        self.assertTrue(any(run.glob("git/*/plastic_stats.json")))
        self.assertTrue((run / "vcs_extra_metrics.csv").is_file())
        for path in run.rglob("*"):
            if path.is_file() and path.suffix in {".json", ".csv", ".log", ".txt"}:
                self.assertNotIn("S3cret!", path.read_text(errors="replace"), str(path))
        return r

    def test_download_mode(self) -> None:
        run = self.run_cli()
        r = self.check(run)
        self.assertEqual(r["primary_language"], "Python")
        self.assertFalse((run / "clones").exists() and any((run / "clones").iterdir()))

    def test_local_workspace_mode(self) -> None:
        root = Path(tempfile.mkdtemp(dir=self.tmp.name))
        (root / "game").mkdir()
        (root / "game" / "x.js").write_text("var a = 1;\n")
        run = self.run_cli("--plastic-local-root", str(root))
        r = self.check(run)
        self.assertEqual(r["primary_language"], "JavaScript")
        self.assertTrue((root / "game" / "x.js").is_file())  # the user's folder is left alone

    def test_rejects_mixed_vcs(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(Path(ex.__file__)), "--plastic-server", "h:8087", "--plastic-repo", "r", "--hg"],
            capture_output=True, text=True, timeout=60,
        )
        self.assertNotEqual(proc.returncode, 0)


if __name__ == "__main__":
    unittest.main()
