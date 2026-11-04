#!/usr/bin/env python3
"""Perforce history metrics: path handling, describe parsing, stats, churn, integrations, auth, end-to-end.

The server tests start a throw-away local ``p4d`` (skipped when ``p4``/``p4d`` are not installed), so
they exercise the real CLI rather than canned output.
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import extract_org_raw_data as ex
import p4_metrics as p4

HAVE_P4 = shutil.which("p4") is not None and shutil.which("p4d") is not None
PASSWORD = "S3cretPass!9"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class P4Server:
    """A local p4d with a small multi-user history; ``secure`` switches on security level 1 (every command needs a password or ticket)."""

    def __init__(self, root: Path, secure: bool = False) -> None:
        self.root = root
        self.port = f"127.0.0.1:{_free_port()}"
        self.secure = secure
        (root / "srv").mkdir()
        self._start()
        self._build()
        if secure:
            self.p4("alice", "passwd", "-P", PASSWORD, client="")
            ticket = self.p4("alice", "login", "-p", stdin=PASSWORD + "\n", client="").split()[-1]
            self.p4("alice", "configure", "set", "security=1", client="", passwd=ticket)
            self.stop()
            self._start()

    def _start(self) -> None:
        subprocess.run(
            ["p4d", "-r", str(self.root / "srv"), "-p", self.port, "-d", "-L", str(self.root / "log")],
            check=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )  # not capture_output: the daemon inherits the pipe and would block the read forever
        for _ in range(50):
            try:
                with socket.create_connection(self.port.split(":"), timeout=0.2):
                    return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("p4d did not start")

    def stop(self) -> None:
        subprocess.run(["pkill", "-f", f"p4d -r {self.root / 'srv'}"], capture_output=True)
        time.sleep(0.5)

    def env(self, user: str, client: str) -> dict[str, str]:
        return {**os.environ, "P4PORT": self.port, "P4USER": user, "P4CLIENT": client, "P4CONFIG": "", "P4ENVIRO": os.devnull,
                "P4TICKETS": str(self.root / "tickets")}  # keep test tickets out of ~/.p4tickets

    def p4(self, user: str, *args: str, client: str | None = None, stdin: str | None = None, cwd: Path | None = None, passwd: str = "") -> str:
        client = f"ws_{user}" if client is None else client
        env = self.env(user, client)
        if passwd:
            env["P4PASSWD"] = passwd
        if cwd is not None:
            env["PWD"] = str(cwd)  # p4 resolves relative paths from $PWD, not from the process cwd
        proc = subprocess.run(["p4", *args], input=stdin, capture_output=True, text=True, cwd=cwd, env=env)
        if proc.returncode:
            raise RuntimeError(f"p4 {args}: {proc.stdout}{proc.stderr}")
        return proc.stdout

    def _ws(self, user: str) -> Path:
        ws = self.root / f"ws_{user}"
        ws.mkdir(exist_ok=True)
        spec = self.p4(user, "client", "-o")
        lines = []
        for line in spec.splitlines():
            if line.startswith("Root:"):
                line = f"Root:\t{ws}"
            elif line.startswith("Options:"):
                line = "Options:\tallwrite noclobber compress unlocked nomodtime normdir"
            lines.append(line)
        self.p4(user, "client", "-i", stdin="\n".join(lines) + "\n")
        return ws

    def _submit(self, user: str, ws: Path, msg: str) -> None:
        self.p4(user, "submit", "-d", msg, cwd=ws)

    def _build(self) -> None:
        a, b, c = self._ws("alice"), self._ws("bob"), self._ws("dependabot-bot")
        (a / "main/src").mkdir(parents=True)
        (a / "main/tests").mkdir(parents=True)
        (a / "main/src/app.py").write_text("def a():\n    return 1\n\n\nclass A:\n    pass\n")
        (a / "main/tests/test_app.py").write_text("def test_a():\n    assert 1\n")
        (a / "main/README.md").write_text("# readme\n")
        (a / "other").mkdir()
        (a / "other/x.txt").write_text("outside the main path\n")
        self.p4("alice", "add", "main/src/app.py", "main/tests/test_app.py", "main/README.md", "other/x.txt", cwd=a)
        self._submit("alice", a, "initial import")  # 1
        (a / "main/src/app.py").write_text("def a():\n    return 2\n\n\nclass A:\n    pass\n\n\ndef b():\n    return 3\n")
        self.p4("alice", "edit", "main/src/app.py", cwd=a)
        self._submit("alice", a, "add b, change a")  # 2
        self.p4("bob", "sync", "-q", "//depot/main/...", cwd=b)
        (b / "main/src/util.py").write_text("x=1\n")
        self.p4("bob", "add", "main/src/util.py", cwd=b)
        self.p4("bob", "edit", "main/README.md", cwd=b)
        (b / "main/README.md").write_text("# readme\nmore\ntext\n")
        self._submit("bob", b, "bob adds util + docs")  # 3
        (b / "main/logo.bin").write_bytes(bytes(range(256)) * 8)
        self.p4("bob", "add", "-t", "binary", "main/logo.bin", cwd=b)
        self._submit("bob", b, "add binary logo")  # 4
        self.p4("bob", "integ", "-q", "//depot/main/...", "//depot/rel/...", cwd=b)
        self._submit("bob", b, "branch rel from main")  # 5
        self.p4("bob", "delete", "main/tests/test_app.py", cwd=b)
        self._submit("bob", b, "remove test")  # 6
        self.p4("dependabot-bot", "sync", "-q", "//depot/main/...", cwd=c)
        self.p4("dependabot-bot", "edit", "main/README.md", cwd=c)
        (c / "main/README.md").write_text("# readme\nbuild 1\n")
        self._submit("dependabot-bot", c, "ci: bump build")  # 7

    def conn(self, user: str = "alice", password: str = "") -> p4.P4Conn:
        return p4.P4Conn(self.port, user, password)


class PathTests(unittest.TestCase):
    def test_normalize_and_slug(self) -> None:
        for raw in ("//depot/proj", "//depot/proj/", "//depot/proj/...", " //depot/proj/... "):
            self.assertEqual(p4.normalize_depot_path(raw), "//depot/proj/...")
        self.assertEqual(p4.path_prefix("//depot/proj"), "//depot/proj/")
        self.assertEqual(p4.slug_for("//depot/a/b/..."), "depot/a/b")
        with self.assertRaises(ValueError):
            p4.normalize_depot_path("depot/proj")

    def test_targets_keep_same_named_projects_apart(self) -> None:
        targets = ex.build_p4_targets("srv:1666", "u", ["//depot/a/src", "//depot/b/src", "//depot/a/src/..."])
        self.assertEqual(len(targets), 2)  # the repeated path collapses
        self.assertTrue(all(t.vcs == "p4" and t.platform == "local" and t.local_path is None for t in targets))
        self.assertEqual({ex.target_row_key(t) for t in targets}.__len__(), 2)
        whole = ex.build_p4_targets("srv:1666", "u", ["//depot"])[0]
        self.assertEqual((whole.org, whole.full_name), ("depot", "depot/depot"))

    def test_serialization_round_trip_keeps_no_secret(self) -> None:
        t = ex.build_p4_targets("srv:1666", "u", ["//depot/a"])
        data = ex.serialize_targets(t)
        self.assertEqual(data[0]["vcs"], "p4")
        self.assertNotIn("password", json.dumps(data).lower())
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "repos.json").write_text(json.dumps(data))
            back = ex.load_targets_from_repos_json(Path(d) / "repos.json")
        assert back is not None
        self.assertEqual(back[0].vcs, "p4")
        self.assertEqual(back[0].meta["p4"]["depot_path"], "//depot/a/...")


class DescribeParseTests(unittest.TestCase):
    TEXT = (
        "Change 3 by bob@ws on 2026/01/01 10:00:00\n\n\tnote\n\nAffected files ...\n\n"
        "... //depot/main/README.md#2 edit\n... //depot/main/src/util.py#1 add\n\nDifferences ...\n\n"
        "==== //depot/main/README.md#2 (text) ====\n\n@@ -1,1 +1,3 @@\n # readme\n+more\n+text\n"
        "Change 4 by bob@ws on 2026/01/02 10:00:00\n\n\t--- a dash subject\n\nAffected files ...\n\n"
        "... //depot/main/a.txt#2 edit\n\nDifferences ...\n\n==== //depot/main/a.txt#2 (text) ====\n\n"
        "@@ -1,2 +1,2 @@\n-old\n+new\n--- a deleted line starting with dashes\n+++ an added line starting with plusses\n"
    )

    def test_parse(self) -> None:
        parsed = p4.parse_describe(self.TEXT)
        self.assertEqual(sorted(parsed), [3, 4])
        self.assertEqual([f["action"] for f in parsed[3]["files"]], ["edit", "add"])
        self.assertEqual(parsed[3]["diffs"]["//depot/main/README.md"], [2, 0])
        # lines that merely *look* like diff headers still count once inside a hunk
        self.assertEqual(parsed[4]["diffs"]["//depot/main/a.txt"], [2, 2])


@unittest.skipUnless(HAVE_P4, "p4 / p4d not installed")
class P4ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.srv = P4Server(Path(cls._tmp.name))
        cls.conn = cls.srv.conn()
        p4.connect(cls.conn)
        cls.changes = list(p4.iter_changes(cls.conn, "//depot/main"))
        cls.stats = p4.aggregate_p4_stats(cls.conn, "//depot/main", ex.is_bot_author, changes=cls.changes)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.srv.stop()
        cls._tmp.cleanup()

    def test_scope_is_the_depot_path(self) -> None:
        # change 1 also added other/x.txt, but only touches //depot/main for this target
        self.assertEqual([c["change"] for c in self.changes], [7, 6, 4, 3, 2, 1])
        other = list(p4.iter_changes(self.conn, "//depot/other"))
        self.assertEqual([c["change"] for c in other], [1])

    def test_totals_and_authors(self) -> None:
        s = self.stats
        self.assertEqual(s["vcs"], "p4")
        self.assertEqual(s["total_commits"], 6)
        self.assertEqual(s["human_authors"], 2)
        self.assertEqual(s["bot_authors"], 1)
        self.assertEqual(s["bot_commit_count"], 1)
        self.assertEqual(s["commits_per_user"], {"bob": 3, "alice": 2, "dependabot-bot": 1})
        self.assertIsNotNone(s["first_commit"])
        self.assertEqual(s["depot_path"], "//depot/main/...")

    def _export(self, path: str = "//depot/main", **kw):
        changes = list(p4.iter_changes(self.conn, path))
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "commits.jsonl"
            n, integ = p4.export_commits_jsonl(
                self.conn, path, out, changes=changes, is_bot=ex.is_bot_author,
                write_json=lambda p, v: p.write_text(json.dumps(v)), **kw,
            )
            recs = [json.loads(line) for line in out.read_text().splitlines()]
            meta = (Path(d) / "commits_detail_meta.json")
            return n, integ, recs, json.loads(meta.read_text()) if meta.exists() else None

    def test_churn(self) -> None:
        n, integ, recs, _ = self._export()
        self.assertEqual(n, 6)
        by = {r["sha"]: r for r in recs}
        self.assertEqual((by["1"]["additions"], by["1"]["deletions"]), (9, 0))  # adds counted via p4 print
        self.assertEqual([f["path"] for f in by["1"]["files"]], ["README.md", "src/app.py", "tests/test_app.py"])
        self.assertEqual((by["2"]["additions"], by["2"]["deletions"]), (5, 1))  # an edit, from the diff
        self.assertEqual((by["3"]["additions"], by["3"]["deletions"]), (3, 0))  # edit +2 and an add +1
        self.assertEqual((by["4"]["additions"], by["4"]["deletions"]), (0, 0))  # binary
        self.assertEqual((by["6"]["additions"], by["6"]["deletions"]), (0, 2))  # a deleted file
        self.assertEqual((by["7"]["additions"], by["7"]["deletions"]), (1, 2))
        self.assertTrue(by["7"]["is_bot"])
        self.assertFalse(by["1"]["is_bot"])
        self.assertEqual(integ, [])

    def test_integrations_are_the_merged_prs_and_carry_no_files(self) -> None:
        _, integ, recs, _ = self._export("//depot/rel")
        self.assertEqual([i["sha"] for i in integ], ["5"])
        self.assertEqual(integ[0]["subject"], "branch rel from main")
        self.assertEqual(recs[0]["files"], [])  # a branch copy is not authored churn

    def test_detail_cap_keeps_totals_and_integration_detection(self) -> None:
        n, integ, recs, meta = self._export("//depot/rel", max_commits=0)
        self.assertEqual(n, 1)
        n, integ, recs, meta = self._export("//depot/main", max_commits=2)
        self.assertEqual(n, 6)
        self.assertEqual([len(r["files"]) > 0 for r in recs], [True, True, False, False, False, False])
        assert meta is not None
        self.assertTrue(meta["capped"])
        self.assertEqual(meta["detail_commits_exported"], 2)

    def test_sync_working_tree_and_client_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d) / "tree"
            files = p4.sync_working_tree(self.conn, "//depot/main", dest)
            self.assertEqual(files, 4)  # README, app.py, util.py, logo.bin (test_app.py was deleted)
            self.assertEqual(sorted(p.name for p in dest.rglob("*") if p.is_file()),
                             ["README.md", "app.py", "logo.bin", "util.py"])
        clients = p4.run_marshal(self.conn, "clients", "-e", "datalabs-*")
        self.assertEqual(clients, [])  # the throw-away client is gone

    def test_branch_origin_flags_a_branched_copy(self) -> None:
        rel = p4.branch_origin(self.conn, "//depot/rel", tree_files=5)
        self.assertEqual(rel["branch_copy_pct"], 100.0)
        self.assertEqual(rel["integration_sources"], ["//depot/main (5)"])
        main = p4.branch_origin(self.conn, "//depot/main", tree_files=4)
        self.assertEqual((main["branch_copy_pct"], main["integration_sources"]), (0.0, []))

    def test_discovery(self) -> None:
        self.assertEqual([d["name"] for d in p4.list_depots(self.conn)], ["depot"])
        self.assertEqual(p4.list_top_dirs(self.conn, "depot"), ["//depot/main", "//depot/other", "//depot/rel"])

    def test_unreachable_server_is_a_clear_error(self) -> None:
        with self.assertRaises(p4.P4Error):
            p4.connect(p4.P4Conn("127.0.0.1:1", "alice"))


@unittest.skipUnless(HAVE_P4, "p4 / p4d not installed")
class P4AuthAndEndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name)
        cls.srv = P4Server(cls.root, secure=True)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.srv.stop()
        cls._tmp.cleanup()

    def test_wrong_password_is_rejected_without_echoing_it(self) -> None:
        with self.assertRaises(p4.P4Error) as ctx:
            p4.connect(self.srv.conn(password="not-the-password"))
        self.assertNotIn("not-the-password", str(ctx.exception))

    def test_password_becomes_a_ticket_and_stays_out_of_repr(self) -> None:
        conn = self.srv.conn(password=PASSWORD)
        p4.connect(conn)
        self.assertTrue(conn.ticket)
        self.assertNotIn(PASSWORD, repr(conn))
        self.assertNotIn(conn.ticket, repr(conn))
        self.assertEqual(len(list(p4.iter_changes(conn, "//depot/main"))), 6)

    def test_cli_end_to_end(self) -> None:
        out = self.root / "out"
        env = {**os.environ, "P4_PASSWORD": PASSWORD}
        for k in ("P4PASSWD", "P4_TICKET"):
            env.pop(k, None)
        proc = subprocess.run(
            [sys.executable, str(Path(ex.__file__)), "--p4-port", self.srv.port, "--p4-user", "alice",
             "--p4-path", "//depot/main", "--p4-path", "//depot/rel", "--output-dir", str(out), "--no-for-check"],
            capture_output=True, text=True, env=env, timeout=300,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        run = next(p for p in out.iterdir() if p.is_dir())
        rows = {r["repo"]: r for r in csv.DictReader((run / "summary.csv").open())}
        self.assertEqual(set(rows), {"main", "rel"})
        main, rel = rows["main"], rows["rel"]
        self.assertEqual((main["error"], rel["error"]), ("", ""))
        self.assertEqual((main["total_commits"], main["merged_prs"]), ("6", "0"))
        self.assertEqual((rel["total_commits"], rel["merged_prs"]), ("1", "1"))
        self.assertEqual(main["measured_branch"], "//depot/main/...")
        self.assertEqual(main["primary_language"], "Python")
        self.assertEqual(main["duplicate_pct"], "")  # changelist numbers are server-local
        self.assertTrue((run / "git/depot__main/p4_stats.json").is_file())
        with (run / "vcs_extra_metrics.csv").open() as fh:
            extra = {r["repo"]: r for r in csv.DictReader(fh)}
        self.assertEqual(extra["depot/rel"]["branch_copy_pct"], "100.0")
        self.assertEqual(extra["depot/rel"]["integration_sources"], "//depot/main (5)")
        self.assertEqual(extra["depot/main"]["branch_copy_pct"], "0.0")
        self.assertEqual(extra["depot/main"]["vcs"], "p4")
        self.assertEqual(extra["depot/main"]["total_commits"], "6")
        self.assertEqual(extra["depot/main"]["median_lines_per_change"], "3.0")
        self.assertGreater(float(extra["depot/main"]["binary_bytes_pct"]), 90)  # logo.bin dominates by size
        manifest = json.loads((run / "manifest.json").read_text())
        self.assertEqual(manifest["vcs"], "perforce")
        self.assertFalse((run / "clones").exists() and any((run / "clones").iterdir()))
        # the secret reaches the child through the environment only
        for path in run.rglob("*"):
            if path.is_file() and path.suffix in {".json", ".csv", ".log", ".txt"}:
                self.assertNotIn(PASSWORD, path.read_text(errors="replace"), str(path))


if __name__ == "__main__":
    unittest.main()
