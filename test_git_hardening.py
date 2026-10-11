#!/usr/bin/env python3
"""Untrusted repos must not get to run their own hooks/fsmonitor on our host."""

from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

import extract_org_raw_data as ex

GIT_ENV = {
    **os.environ, "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@x", "GIT_COMMITTER_NAME": "a",
    "GIT_COMMITTER_EMAIL": "a@x",
}


def _git(cwd: Path, *args: str, date: str | None = None) -> None:
    env = dict(GIT_ENV)
    if date:
        env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, env=env)


class HookDoesNotRunTests(unittest.TestCase):
    def test_post_checkout_hook_and_fsmonitor_do_not_execute(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            origin, clone, marker = root / "origin", root / "clone", root / "MARKER"
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
            subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True, capture_output=True)

            hook = clone / ".git" / "hooks" / "post-checkout"
            hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
            hook.chmod(hook.stat().st_mode | stat.S_IXUSR)
            fsmon = root / "fsmon.sh"
            fsmon.write_text(f"#!/bin/sh\ntouch {marker}\n")
            fsmon.chmod(0o755)
            _git(clone, "config", "core.fsmonitor", str(fsmon))

            self.assertEqual(ex.select_latest_branch(clone, "main", None), "newer")
            ex.run_git(clone, "status")
            ex.aggregate_git_stats(clone)
            self.assertFalse(marker.exists(), "repo-shipped hook/fsmonitor executed")


if __name__ == "__main__":
    unittest.main()
