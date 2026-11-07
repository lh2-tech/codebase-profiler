#!/usr/bin/env python3
"""The live-log download serves the whole current run log, and only from inside the output folder."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import extract_ui as ui


class LiveLogTests(unittest.TestCase):
    def test_serves_the_run_log_while_the_run_is_going(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            run = out / "raw-extract-x"
            (run / "logs").mkdir(parents=True)
            (run / "logs" / "extract.log").write_text("line 1\nline 2\n")
            with mock.patch.object(ui, "DEFAULT_OUTPUT", out), mock.patch.dict(ui.STATE, {"run_dir": str(run)}):
                self.assertEqual(ui.live_log_path(), run / "logs" / "extract.log")
                (run / "logs" / "extract.log").write_text("line 1\nline 2\nline 3\n")  # the run keeps appending
                self.assertEqual(ui.live_log_path().read_text().count("\n"), 3)

    def test_no_run_or_no_log_or_a_path_outside_outputs_gives_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as other:
            out = Path(d)
            with mock.patch.object(ui, "DEFAULT_OUTPUT", out):
                with mock.patch.dict(ui.STATE, {"run_dir": None}):
                    self.assertIsNone(ui.live_log_path())
                with mock.patch.dict(ui.STATE, {"run_dir": str(out / "nope")}):
                    self.assertIsNone(ui.live_log_path())
                (Path(other) / "logs").mkdir()
                (Path(other) / "logs" / "extract.log").write_text("secret")
                with mock.patch.dict(ui.STATE, {"run_dir": other}):  # a run dir outside the output folder is refused
                    self.assertIsNone(ui.live_log_path())


class WorkspaceDirTests(unittest.TestCase):
    def test_default_follows_env_then_docker_mode(self) -> None:
        with mock.patch.dict("os.environ", {"DEFAULT_P4_WORKSPACES_DIR": "/somewhere"}):
            self.assertEqual(ui.default_p4_workspaces_dir(), "/somewhere")
        with mock.patch.dict("os.environ", {}, clear=False) as env:
            env.pop("DEFAULT_P4_WORKSPACES_DIR", None)
            with mock.patch.object(ui, "is_docker_mode", return_value=True):
                self.assertEqual(ui.default_p4_workspaces_dir(), "/data/p4-workspaces")


if __name__ == "__main__":
    unittest.main()
